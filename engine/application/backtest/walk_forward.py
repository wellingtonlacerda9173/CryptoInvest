import itertools
import math
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Self

from pydantic import Field, model_validator

from engine.application.backtest.engine import BacktestEngine, Clock, _utc_now
from engine.application.backtest.metrics import Metrics, compute_metrics, metrics_from_parts
from engine.application.backtest.models import BacktestResult, EquityPoint, Record, RoundTrip
from engine.application.backtest.settings import BacktestSettings
from engine.application.risk import EventCalendar, SymbolRules
from engine.application.strategies import Strategy, StrategyContext, StrategySignal
from engine.domain.candle import Candle

Params = Mapping[str, Decimal]
StrategyFactory = Callable[[Params], Sequence[Strategy]]

_DRAWDOWN_BUCKET = 0.005
_MIN_OOS_TRADES = 30
_MAX_DEGRADATION = 0.5
_DRAWDOWN_WORSENING = 1.5
_UNSTABLE_SELECTION = 0.5
_MONTH_DAYS = 30


class NotEnoughCandlesError(ValueError):
    """O periodo nao comporta nenhuma janela completa de treino + teste."""


class WalkForwardConfig(Record):
    """Janelas rolantes sequenciais: treino [t, t+train), teste [t+train, t+train+test), avanco de `step`.

    O teste nunca se sobrepoe: `step_days` >= `test_days` (padrao = `test_days`, testes contiguos).
    """

    train_days: int = Field(default=180, ge=7)
    test_days: int = Field(default=60, ge=1)
    step_days: int | None = Field(default=None, ge=1)
    min_train_trades: int = Field(default=5, ge=1)
    require_positive_train: bool = True

    @model_validator(mode="after")
    def _check_step(self) -> Self:
        if self.step_days is not None and self.step_days < self.test_days:
            raise ValueError("step_days must be >= test_days (test windows must not overlap)")
        return self

    @property
    def step(self) -> int:
        return self.step_days or self.test_days


class ParameterGrid(Record):
    values: dict[str, tuple[Decimal, ...]]

    def combinations(self) -> list[dict[str, Decimal]]:
        names = sorted(self.values)
        return [
            dict(zip(names, combo, strict=True))
            for combo in itertools.product(*(self.values[n] for n in names))
        ]


def combo_label(params: Params) -> str:
    return ",".join(f"{name}={value}" for name, value in sorted(params.items()))


class MemoizedStrategy:
    """Reaproveita a avaliacao da MESMA estrategia/parametros sobre a MESMA janela de velas entre as janelas
    do walk-forward (a saida depende apenas das velas ate a ultima): acelera sem alterar nenhum resultado."""

    def __init__(
        self, inner: Strategy, key: str, store: dict[tuple[str, datetime, int], StrategySignal | None]
    ) -> None:
        self.name = inner.name
        self._inner = inner
        self._key = key
        self._store = store

    def evaluate(self, candles: Sequence[Candle], context: StrategyContext) -> StrategySignal | None:
        key = (self._key, candles[-1].open_time, len(candles))
        if key not in self._store:
            self._store[key] = self._inner.evaluate(candles, context)
        return self._store[key]


class Candidate(Record):
    params: dict[str, Decimal]
    trades: int
    max_drawdown: float
    positive_months_ratio: float | None
    total_return: float
    eligible: bool


class WindowResult(Record):
    index: int
    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime
    candidates: tuple[Candidate, ...]
    selected: dict[str, Decimal] | None
    train_metrics: Metrics | None
    test_metrics: Metrics | None
    test_result: BacktestResult | None


class OverfitAssessment(Record):
    in_sample_return_per_30d: float | None
    out_of_sample_return_per_30d: float | None
    in_sample_expectancy_r: float | None
    out_of_sample_expectancy_r: float | None
    in_sample_mean_drawdown: float | None
    out_of_sample_drawdown: float
    distinct_selections: int
    flags: tuple[str, ...]
    verdict: str


class WalkForwardResult(Record):
    config: WalkForwardConfig
    grid: ParameterGrid
    windows: tuple[WindowResult, ...]
    out_of_sample: Metrics
    out_of_sample_curve: tuple[EquityPoint, ...]
    out_of_sample_round_trips: tuple[RoundTrip, ...]
    assessment: OverfitAssessment


def _window_bounds(total: int, per_day: int, config: WalkForwardConfig) -> list[tuple[int, int, int]]:
    train, test, step = config.train_days * per_day, config.test_days * per_day, config.step * per_day
    bounds, start = [], 0
    while start + train + test <= total:
        bounds.append((start, start + train, start + train + test))
        start += step
    return bounds


def _selection_key(index: int, candidate: Candidate) -> tuple[int, float, float, int]:
    bucket = math.ceil(candidate.max_drawdown / _DRAWDOWN_BUCKET)
    return bucket, -(candidate.positive_months_ratio or 0.0), -candidate.total_return, index


def _scaled(trip: RoundTrip, factor: Decimal) -> RoundTrip:
    return trip.model_copy(update={"pnl": trip.pnl * factor, "fees": trip.fees * factor})


async def walk_forward(
    candles: Sequence[Candle],
    factory: StrategyFactory,
    grid: ParameterGrid,
    settings: BacktestSettings,
    config: WalkForwardConfig,
    *,
    rules: SymbolRules | None = None,
    calendar: EventCalendar | None = None,
    clock: Clock = _utc_now,
) -> WalkForwardResult:
    """Em cada janela: escolhe os parametros SO no treino (drawdown, depois consistencia, depois retorno) e os
    avalia na janela de teste seguinte, com capital reiniciado. Nada do teste entra na escolha; as velas
    anteriores ao inicio de cada janela servem apenas de aquecimento dos indicadores."""
    per_day = 86_400 // candles[0].timeframe.seconds
    bounds = _window_bounds(len(candles), per_day, config)
    if not bounds:
        needed = (config.train_days + config.test_days) * per_day
        raise NotEnoughCandlesError(
            f"periodo curto demais para o walk-forward: {len(candles)} velas, sao necessarias ao menos "
            f"{needed} (treino {config.train_days} + teste {config.test_days} dias)"
        )
    combos = grid.combinations()
    warmup = settings.history_window
    store: dict[tuple[str, datetime, int], StrategySignal | None] = {}

    async def run(params: Params, begin: int, end: int) -> BacktestResult:
        strategies = [MemoizedStrategy(s, f"{s.name}|{combo_label(params)}", store) for s in factory(params)]
        lo = max(0, begin - warmup)
        engine = BacktestEngine(strategies, settings, rules=rules, calendar=calendar, clock=clock)
        return await engine.run(candles[lo:end], start=begin - lo)

    windows: list[WindowResult] = []
    for number, (train_start, train_end, test_end) in enumerate(bounds, start=1):
        scored: list[tuple[Candidate, Metrics]] = []
        for params in combos:
            metrics = compute_metrics(await run(params, train_start, train_end))
            eligible = metrics.trades >= config.min_train_trades and (
                not config.require_positive_train or metrics.total_return > 0
            )
            scored.append(
                (
                    Candidate(
                        params=params,
                        trades=metrics.trades,
                        max_drawdown=metrics.max_drawdown,
                        positive_months_ratio=metrics.positive_months_ratio,
                        total_return=metrics.total_return,
                        eligible=eligible,
                    ),
                    metrics,
                )
            )
        ranked = [(i, c, m) for i, (c, m) in enumerate(scored) if c.eligible]
        best = min(ranked, key=lambda item: _selection_key(item[0], item[1]), default=None)
        test_result = await run(best[1].params, train_end, test_end) if best else None
        windows.append(
            WindowResult(
                index=number,
                train_start=candles[train_start].open_time,
                train_end=candles[train_end - 1].close_time,
                test_start=candles[train_end].open_time,
                test_end=candles[test_end - 1].close_time,
                candidates=tuple(c for c, _ in scored),
                selected=best[1].params if best else None,
                train_metrics=best[2] if best else None,
                test_metrics=compute_metrics(test_result) if test_result else None,
                test_result=test_result,
            )
        )
    return _aggregate(candles, bounds, windows, grid, config, settings)


def _aggregate(
    candles: Sequence[Candle],
    bounds: list[tuple[int, int, int]],
    windows: list[WindowResult],
    grid: ParameterGrid,
    config: WalkForwardConfig,
    settings: BacktestSettings,
) -> WalkForwardResult:
    initial = settings.initial_capital
    capital = initial
    curve: list[EquityPoint] = []
    trips: list[RoundTrip] = []
    for (_, train_end, test_end), window in zip(bounds, windows, strict=True):
        factor = capital / initial
        if window.test_result is None:
            curve.extend(
                EquityPoint(at=c.close_time, equity=capital, realized=capital, in_position=False)
                for c in candles[train_end:test_end]
            )
            continue
        curve.extend(
            p.model_copy(update={"equity": p.equity * factor, "realized": p.realized * factor})
            for p in window.test_result.equity_curve
        )
        trips.extend(_scaled(t, factor) for t in window.test_result.round_trips)
        capital = curve[-1].equity
    oos = metrics_from_parts(initial, curve, trips)
    return WalkForwardResult(
        config=config,
        grid=grid,
        windows=tuple(windows),
        out_of_sample=oos,
        out_of_sample_curve=tuple(curve),
        out_of_sample_round_trips=tuple(trips),
        assessment=_assess(windows, oos, config),
    )


def _per_30d(total_return: float, days: int) -> float:
    return total_return / days * _MONTH_DAYS


def _assess(windows: Sequence[WindowResult], oos: Metrics, config: WalkForwardConfig) -> OverfitAssessment:
    trained = [w.train_metrics for w in windows if w.train_metrics is not None]
    expectancies = [m.expectancy_r for m in trained if m.expectancy_r is not None]
    in_return = (
        sum(_per_30d(m.total_return, config.train_days) for m in trained) / len(trained) if trained else None
    )
    in_dd = sum(m.max_drawdown for m in trained) / len(trained) if trained else None
    in_exp = sum(expectancies) / len(expectancies) if expectancies else None
    total_days = config.test_days * len(windows)
    out_return = _per_30d(oos.total_return, total_days)
    selections = {tuple(sorted(w.selected.items())) for w in windows if w.selected is not None}
    chosen = sum(1 for w in windows if w.selected is not None)

    flags: list[str] = []
    if in_exp is not None and in_exp > 0 and (oos.expectancy_r is None or oos.expectancy_r <= 0):
        flags.append("expectativa em R positiva no treino e nao positiva fora da amostra")
    if in_return is not None and in_return > 0 and out_return < _MAX_DEGRADATION * in_return:
        flags.append("retorno mensal fora da amostra abaixo de metade do retorno no treino")
    if in_dd is not None and oos.max_drawdown > _DRAWDOWN_WORSENING * in_dd:
        flags.append("drawdown fora da amostra maior que 1,5x o drawdown medio do treino")
    if chosen >= 2 and len(selections) / chosen > _UNSTABLE_SELECTION:
        flags.append("parametros escolhidos mudam quase a cada janela (selecao instavel)")
    in_edge = in_exp is not None and in_exp > 0 and in_return is not None and in_return > 0
    out_edge = oos.expectancy_r is not None and oos.expectancy_r > 0 and out_return > 0
    if chosen == 0:
        verdict = "sem selecao: nenhum parametro passou no treino; o robo ficaria parado"
    elif oos.trades < _MIN_OOS_TRADES:
        verdict = "inconclusivo (amostra pequena)"
        flags.insert(0, f"apenas {oos.trades} trades fora da amostra (minimo {_MIN_OOS_TRADES})")
    elif not out_edge:
        verdict = "indicios de sobreajuste" if in_edge else "sem edge detectavel fora da amostra"
    elif in_edge and in_return is not None and out_return < _MAX_DEGRADATION * in_return:
        verdict = "indicios de sobreajuste"
    else:
        verdict = "consistente"
    return OverfitAssessment(
        in_sample_return_per_30d=in_return,
        out_of_sample_return_per_30d=out_return,
        in_sample_expectancy_r=in_exp,
        out_of_sample_expectancy_r=oos.expectancy_r,
        in_sample_mean_drawdown=in_dd,
        out_of_sample_drawdown=oos.max_drawdown,
        distinct_selections=len(selections),
        flags=tuple(flags),
        verdict=verdict,
    )
