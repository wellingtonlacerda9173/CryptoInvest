from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from engine.application.backtest import BacktestSettings
from engine.application.backtest.engine import Clock, _utc_now
from engine.application.regime import Regime
from engine.application.risk import EventCalendar, SymbolRules
from engine.application.selector import (
    SelectionDecision,
    TradeSample,
    build_scores,
    select_strategy,
)
from engine.application.strategies import Strategy
from engine.application.validation.collect import collect_runs
from engine.application.validation.idle import Span, exposure, fraction_of
from engine.application.validation.models import (
    EntryReport,
    ValidationConfig,
    ValidationReport,
    WindowReport,
)
from engine.application.validation.references import (
    Window,
    buy_and_hold,
    random_reference,
    trade_stats,
)
from engine.application.validation.verdict import judge
from engine.domain.candle import Candle

OPERABLE_REGIMES = (Regime.TENDENCIA_ALTA, Regime.TENDENCIA_BAIXA, Regime.LATERAL)


class NotEnoughHistoryError(ValueError):
    """Historico curto demais para treino minimo + uma janela de teste."""


def window_bounds(candles: Sequence[Candle], config: ValidationConfig) -> list[Window]:
    per_day = 86_400 // candles[0].timeframe.seconds
    first, size = config.train_days_min * per_day, config.test_days * per_day
    bounds = [(s, s + size) for s in range(first, len(candles) - size + 1, size)]
    if not bounds:
        raise NotEnoughHistoryError(
            f"são necessárias ao menos {first + size} velas ({config.train_days_min} dias de treino + "
            f"{config.test_days} de teste); há {len(candles)}"
        )
    return bounds


def _scaled(sample: TradeSample, weight: float) -> TradeSample:
    return sample.model_copy(
        update={"r_multiple": sample.r_multiple * weight, "return_frac": sample.return_frac * weight}
    )


def selector_walk_forward(
    candles: Sequence[Candle],
    samples: Sequence[TradeSample],
    strategies: Sequence[str],
    config: ValidationConfig,
) -> tuple[list[WindowReport], list[TradeSample], dict[str, list[TradeSample]], list[Window]]:
    """Em cada janela de teste os scores usam SO trades ENCERRADOS ate o inicio dela (`as_of`).

    Ficar parado (regime sem operacao ou nenhuma estrategia elegivel) conta como resultado zero. Retorna as
    janelas, os trades do seletor (ponderados), os trades OOS de cada estrategia isolada e os limites.
    """
    symbol = candles[0].symbol
    own = [s for s in samples if s.symbol == symbol]
    bounds = window_bounds(candles, config)
    windows: list[WindowReport] = []
    picked: list[TradeSample] = []
    solo: dict[str, list[TradeSample]] = defaultdict(list)
    for number, (start, end) in enumerate(bounds, start=1):
        as_of, test_end = candles[start].open_time, candles[end - 1].close_time
        scores = build_scores(own, as_of, config.selector, lookback_days=config.train_days)
        decisions: dict[Regime, SelectionDecision] = {
            regime: select_strategy(regime, symbol, strategies, scores, config.selector)
            for regime in OPERABLE_REGIMES
        }
        taken: list[TradeSample] = []
        for sample in own:
            if not as_of <= sample.opened_at < test_end:
                continue
            solo[sample.strategy].append(sample)
            if sample.operable and (weight := decisions[sample.regime].weight_of(sample.strategy)):
                taken.append(_scaled(sample, weight))
        picked += taken
        windows.append(
            WindowReport(
                index=number,
                as_of=as_of,
                test_start=as_of,
                test_end=test_end,
                decisions=tuple(decisions.values()),
                selector_trades=len(taken),
                selector_total_r=sum(t.r_multiple for t in taken),
                idle_regimes=sum(1 for d in decisions.values() if not d.operate),
            )
        )
    return windows, picked, solo, bounds


def validate_samples(
    candles: Sequence[Candle],
    samples: Sequence[TradeSample],
    strategies: Sequence[str],
    config: ValidationConfig,
    *,
    generated_at: datetime | None = None,
    idle: Mapping[str, Sequence[Span]] | None = None,
) -> ValidationReport:
    """`idle`: intervalos de bloqueio do gate por estrategia (da coleta); sem ele nao ha censura.

    O seletor nao tem `idle_fraction` (cada janela troca de estrategia): so as estrategias isoladas sao
    julgadas como censuradas.
    """
    windows, picked, solo, bounds = selector_walk_forward(candles, samples, strategies, config)
    spans = [(candles[a].open_time, candles[b - 1].close_time) for a, b in bounds]
    comparisons = len(strategies) + 1 + config.extra_comparisons
    min_trades = config.selector.min_trades
    bh = buy_and_hold(candles, bounds)

    def entry(name: str, items: Sequence[TradeSample], *, solo_run: bool = True) -> EntryReport:
        stats = trade_stats(items)
        reference = random_reference(candles, bounds, items, config)
        stopped = fraction_of(idle[name], spans) if idle is not None and solo_run else None
        return EntryReport(
            name=name,
            stats=stats,
            random_reference=reference,
            judgement=judge(
                stats,
                reference,
                bh,
                config,
                comparisons=comparisons,
                min_trades=min_trades,
                idle_fraction=stopped,
            ),
            idle_fraction=stopped,
            exposure=exposure(items, spans),
        )

    return ValidationReport(
        symbol=candles[0].symbol,
        timeframe=candles[0].timeframe.value,
        period_start=candles[bounds[0][0]].open_time,
        period_end=candles[bounds[-1][1] - 1].close_time,
        generated_at=generated_at or datetime.now(UTC),
        config=config,
        comparisons=comparisons,
        min_trades=min_trades,
        windows=tuple(windows),
        buy_hold=bh,
        strategies=tuple(entry(name, solo.get(name, [])) for name in strategies),
        selector=entry("seletor", picked, solo_run=False),
    )


async def run_validation(
    candles: Sequence[Candle],
    strategies: Sequence[Strategy],
    settings: BacktestSettings,
    config: ValidationConfig = ValidationConfig(),  # noqa: B008
    *,
    rules: SymbolRules | None = None,
    calendar: EventCalendar | None = None,
    clock: Clock = _utc_now,
) -> ValidationReport:
    cost = settings.maker_fee_rate + settings.risk.taker_fee_rate + 2 * settings.slippage_bps / 10_000
    if config.cost_pct is None:
        config = config.model_copy(update={"cost_pct": float(cost)})
    samples, idle = await collect_runs(
        candles, strategies, settings, config.regime, rules=rules, calendar=calendar, clock=clock
    )
    return validate_samples(
        candles, samples, [s.name for s in strategies], config, generated_at=clock(), idle=idle
    )
