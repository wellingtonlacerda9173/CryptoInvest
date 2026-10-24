"""Velas sinteticas deterministicas gravadas no cache local do backtest (nenhum acesso a rede ou banco)."""

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from engine.domain.candle import Candle
from engine.domain.types import Timeframe
from engine.runner.candle_source import cache_path, write_cache


def synthetic_candles(symbol: str, timeframe: Timeframe, start: datetime, count: int) -> list[Candle]:
    """Tendencia suave + ondas + uma barra grande de tempos em tempos (gera sinais de barra elefante)."""
    step = timedelta(seconds=timeframe.seconds)
    candles: list[Candle] = []
    price = 100.0
    for i in range(count):
        drift = 0.04 + 1.2 * math.sin(i / 23.0) * 0.1 + 0.6 * math.sin(i / 7.0) * 0.05
        spike = 3.5 if i % 41 == 40 else 0.0
        open_ = price
        close = max(5.0, open_ + drift + spike)
        high = max(open_, close) + 0.15
        low = min(open_, close) - 0.15
        volume = 3000.0 if spike else 1000.0 + 50 * math.sin(i / 5.0)
        candles.append(
            Candle(
                symbol=symbol,
                timeframe=timeframe,
                open_time=start + step * i,
                open=Decimal(f"{open_:.4f}"),
                high=Decimal(f"{high:.4f}"),
                low=Decimal(f"{low:.4f}"),
                close=Decimal(f"{close:.4f}"),
                volume=Decimal(f"{volume:.4f}"),
                closed=True,
            )
        )
        price = close
    return candles


def write_synthetic_cache(
    cache_dir: Path, symbol: str, timeframe: Timeframe, start: datetime, days: int
) -> list[Candle]:
    per_day = 86_400 // timeframe.seconds
    candles = synthetic_candles(symbol, timeframe, start, days * per_day)
    write_cache(cache_path(cache_dir, symbol, timeframe), candles)
    return candles


def aligned_start(days_ago: int, timeframe: Timeframe = Timeframe.H1) -> datetime:
    now = datetime.now(UTC)
    floor = int(now.timestamp()) // timeframe.seconds * timeframe.seconds
    return datetime.fromtimestamp(floor, tz=UTC) - timedelta(days=days_ago)
