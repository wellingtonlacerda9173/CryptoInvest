from collections.abc import Callable, Sequence
from decimal import Decimal

from engine.application.backtest import BacktestEngine, BacktestSettings, RoundTrip
from engine.application.backtest.engine import Clock, _utc_now
from engine.application.backtest.models import BacktestResult, DecisionAction
from engine.application.regime import Regime, RegimeConfig, RegimeTimeline
from engine.application.risk import EventCalendar, SymbolRules
from engine.application.selector import TradeSample
from engine.application.strategies import Strategy
from engine.application.validation.idle import Span, lock_spans
from engine.domain.candle import Candle

_FALLBACK_RR = 2.0


def samples_from_result(
    result: BacktestResult, timeline: RegimeTimeline, *, source: str = "backtest"
) -> list[TradeSample]:
    """Round-trips -> `TradeSample` (regime conhecido na entrada, distancias de stop/alvo do sinal)."""
    levels = {
        d.order_id: (d.entry, d.stop, d.target)
        for d in result.decisions
        if d.action is DecisionAction.TRADED and d.order_id and d.entry and d.stop and d.target
    }
    initial = result.initial_capital
    return [
        _sample(trip, levels.get(trip.order_id), timeline, float(trip.pnl / initial), source)
        for trip in result.round_trips
    ]


def _sample(
    trip: RoundTrip,
    level: tuple[Decimal, Decimal, Decimal] | None,
    timeline: RegimeTimeline,
    return_frac: float,
    source: str,
) -> TradeSample:
    reading = timeline.at(trip.opened_at)
    stop_pct = target_pct = None
    if level is not None:
        entry, stop, target = (float(x) for x in level)
        stop_pct, target_pct = (entry - stop) / entry, (target - entry) / entry
    elif trip.quantity > 0:
        stop_pct = float(trip.risk_amount / (trip.quantity * trip.entry_price))
    return TradeSample(
        strategy=trip.strategy,
        symbol=trip.symbol,
        regime=reading.regime if reading else Regime.INDEFINIDO,
        operable=reading.operable if reading else False,
        opened_at=trip.opened_at,
        closed_at=trip.closed_at,
        r_multiple=float(trip.r_multiple),
        return_frac=return_frac,
        stop_pct=stop_pct if stop_pct and stop_pct > 0 else None,
        target_pct=target_pct if target_pct and target_pct > 0 else None,
        source=source,
    )


async def collect_runs(
    candles: Sequence[Candle],
    strategies: Sequence[Strategy],
    settings: BacktestSettings,
    regime_config: RegimeConfig,
    *,
    rules: SymbolRules | None = None,
    calendar: EventCalendar | None = None,
    clock: Clock = _utc_now,
    on_done: Callable[[str], None] | None = None,
) -> tuple[list[TradeSample], dict[str, list[Span]]]:
    """Roda CADA estrategia isolada sobre a serie inteira (o motor e causal) e rotula os trades por regime.

    Decisao (so da coleta/validacao; BacktestEngine e PaperRobot em producao seguem com o halt): o halt
    PERMANENTE por drawdown fica DESLIGADO (`BacktestSettings.drawdown_halt=False`). Reiniciar a cada janela
    de teste exigiria rodar o motor por janela, quebrando o contexto das posicoes e a causalidade das
    amostras que o seletor usa; com o halt ligado, uma sequencia ruim cedo encerrava a estrategia para
    sempre e a validacao media o gate, nao o sinal. Continuam valendo stop, R:R minimo, risco por operacao,
    limites de posicao/exposicao, perda diaria, meta diaria, pausa por perdas seguidas. Devolve tambem, por
    estrategia, os intervalos em que alguma trava bloqueou entradas (`lock_spans`), para medir a censura.
    """
    timeline = RegimeTimeline.from_candles(candles, regime_config)
    free = settings.model_copy(update={"drawdown_halt": False})
    samples: list[TradeSample] = []
    idle: dict[str, list[Span]] = {}
    for strategy in strategies:
        engine = BacktestEngine([strategy], free, rules=rules, calendar=calendar, clock=clock)
        result = await engine.run(candles)
        samples += samples_from_result(result, timeline)
        idle[strategy.name] = lock_spans(result.risk_events, result.period_end)
        if on_done is not None:
            on_done(strategy.name)
    return samples, idle


async def collect_samples(
    candles: Sequence[Candle],
    strategies: Sequence[Strategy],
    settings: BacktestSettings,
    regime_config: RegimeConfig,
    *,
    rules: SymbolRules | None = None,
    calendar: EventCalendar | None = None,
    clock: Clock = _utc_now,
) -> list[TradeSample]:
    samples, _ = await collect_runs(
        candles, strategies, settings, regime_config, rules=rules, calendar=calendar, clock=clock
    )
    return samples
