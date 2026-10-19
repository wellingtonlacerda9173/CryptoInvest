import math
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from engine.domain.candle import Candle
from engine.domain.types import Timeframe

T0 = datetime(2026, 1, 1, tzinfo=UTC)
Row = tuple[float, float, float, float, float]


def build_candles(
    rows: Sequence[Row],
    timeframe: Timeframe = Timeframe.M15,
    symbol: str = "BTC/USDT",
    start: datetime = T0,
) -> list[Candle]:
    """Cada linha e (open, high, low, close, volume)."""
    step = timedelta(seconds=timeframe.seconds)
    return [
        Candle(
            symbol=symbol,
            timeframe=timeframe,
            open_time=start + i * step,
            open=Decimal(str(o)),
            high=Decimal(str(h)),
            low=Decimal(str(low)),
            close=Decimal(str(c)),
            volume=Decimal(str(v)),
            closed=True,
        )
        for i, (o, h, low, c, v) in enumerate(rows)
    ]


def candle_row(open_: float, close: float, volume: float = 100.0, wick: float = 0.1) -> Row:
    return (open_, max(open_, close) + wick, min(open_, close) - wick, close, volume)


def wave_rows(count: int, amplitude: float = 1.5) -> tuple[list[Row], float]:
    """Lateralizacao senoidal deterministica em torno de 100 (corpos pequenos, volume 100-108)."""
    rows: list[Row] = []
    previous = 100.0
    for i in range(count):
        close = 100 + amplitude * math.sin(0.5 * i)
        rows.append(candle_row(previous, close, 100 + (i % 5) * 2, 0.15))
        previous = close
    return rows, previous


def reversal_rows() -> list[Row]:
    """45 velas de queda seguidas de 25 de alta forte: cruzamento 9/21 para cima na vela de indice 52."""
    rows: list[Row] = []
    price = 100.0
    for i in range(45):
        nxt = price - 0.4 + 0.15 * math.sin(i)
        rows.append(candle_row(price, nxt, 100 + (i % 3) * 5, 0.2))
        price = nxt
    for i in range(25):
        nxt = price + 0.9 + 0.1 * math.sin(i)
        rows.append(candle_row(price, nxt, 130 + (i % 3) * 5, 0.2))
        price = nxt
    return rows
