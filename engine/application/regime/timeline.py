from bisect import bisect_right
from collections.abc import Sequence
from datetime import datetime
from typing import Self

from engine.application.regime.classifier import DEFAULT_CONFIG, classify_series
from engine.application.regime.models import RegimeConfig, RegimeReading
from engine.domain.candle import Candle


class RegimeTimeline:
    """Regime conhecido num instante: a leitura da ultima vela FECHADA ate ele (nunca da seguinte)."""

    def __init__(self, readings: Sequence[RegimeReading]) -> None:
        self._readings = list(readings)
        self._times = [r.at for r in self._readings]

    @classmethod
    def from_candles(cls, candles: Sequence[Candle], config: RegimeConfig = DEFAULT_CONFIG) -> Self:
        return cls(classify_series(candles, config))

    def at(self, moment: datetime) -> RegimeReading | None:
        index = bisect_right(self._times, moment) - 1
        return self._readings[index] if index >= 0 else None
