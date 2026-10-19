from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from engine.application.backtest import BacktestEngine, BacktestResult, BacktestSettings
from engine.application.strategies import StrategyContext, StrategySignal
from engine.domain.candle import Candle
from engine.domain.risk import RiskConfig
from engine.domain.signal import Signal
from engine.domain.types import Timeframe

D = Decimal
T0 = datetime(2026, 1, 1, tzinfo=UTC)
FIXED_NOW = datetime(2030, 1, 1, tzinfo=UTC)
CONFLUENCES = (
    "volume_forte",
    "preco_acima_da_ema_de_tendencia",
    "rsi_em_zona_de_forca",
    "fechamento_acima_da_maxima_recente",
)
Candles = list[Candle]
Levels = Callable[[Candle], tuple[str, str, str]]


def mk(i: int, o: str, h: str, low: str, c: str, volume: str = "1000") -> Candle:
    return Candle(
        symbol="BTC/USDT",
        timeframe=Timeframe.H1,
        open_time=T0 + timedelta(hours=i),
        open=D(o),
        high=D(h),
        low=D(low),
        close=D(c),
        volume=D(volume),
        closed=True,
    )


def flat_then(rows: Sequence[tuple[str, str, str, str]], warmup: int = 30) -> Candles:
    candles = [mk(i, "100", "100.2", "99.8", "100") for i in range(warmup)]
    candles += [mk(warmup + j, *row) for j, row in enumerate(rows)]
    return candles


def fixed_levels(candle: Candle) -> tuple[str, str, str]:
    return "100", "95", "111"


class Scripted:
    """Estrategia de teste: emite um sinal nas velas indicadas; registra o que enxergou."""

    name = "scripted"

    def __init__(self, at: set[int], levels: Levels = fixed_levels) -> None:
        self.at = at
        self.levels = levels
        self.seen: list[tuple[int, datetime]] = []

    def evaluate(self, candles: Sequence[Candle], context: StrategyContext) -> StrategySignal | None:
        last = candles[-1]
        index = int((last.open_time - T0) / timedelta(hours=1))
        self.seen.append((len(candles), last.close_time))
        if index not in self.at:
            return None
        entry, stop, target = self.levels(last)
        signal = Signal(
            symbol=last.symbol,
            timeframe=last.timeframe,
            created_at=last.close_time,
            strategy=self.name,
            entry=D(entry),
            stop_loss=D(stop),
            take_profit=D(target),
        )
        return StrategySignal(
            signal=signal, confidence=0.8, rationale="teste", triggers=(), confluences=CONFLUENCES
        )


def settings(**kw: object) -> BacktestSettings:
    risk_kw = {k: v for k, v in kw.items() if k in RiskConfig.model_fields}
    rest = {k: v for k, v in kw.items() if k not in RiskConfig.model_fields}
    return BacktestSettings.model_validate({"history_window": 60, "risk": RiskConfig(**risk_kw), **rest})  # type: ignore[arg-type]


async def run(
    candles: Candles, at: set[int] | None = None, levels: Levels = fixed_levels, **kw: object
) -> BacktestResult:
    engine = BacktestEngine(
        [Scripted({29} if at is None else at, levels)], settings(**kw), clock=lambda: FIXED_NOW
    )
    return await engine.run(candles)


ENTRY_CANDLE = ("100", "101", "99.5", "100.5")
QUIET = ("100", "100.1", "99.9", "100")
