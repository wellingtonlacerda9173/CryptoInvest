from collections.abc import Sequence
from itertools import pairwise

from engine.domain.candle import Candle
from engine.domain.types import Timeframe


def candles_are_continuous(candles: Sequence[Candle], timeframe: Timeframe, last: int | None = None) -> bool:
    """Verdadeiro se as `last` velas finais (todas, se None) sao do mesmo ativo e timeframe, sem buracos."""
    tail = candles if last is None else candles[-last:]
    step = timeframe.seconds
    for previous, current in pairwise(tail):
        if current.symbol != previous.symbol:
            return False
        if (current.open_time - previous.open_time).total_seconds() != step:
            return False
    return all(c.timeframe == timeframe for c in tail)
