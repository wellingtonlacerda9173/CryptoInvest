from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

from engine.domain.candle import Candle

FloatArray = npt.NDArray[np.float64]


@dataclass(frozen=True, slots=True)
class CandleArrays:
    """Visao numerica (float64) das velas; indicadores sao estatisticos, o dinheiro continua em Decimal."""

    open: FloatArray
    high: FloatArray
    low: FloatArray
    close: FloatArray
    volume: FloatArray

    @classmethod
    def from_candles(cls, candles: Sequence[Candle]) -> "CandleArrays":
        def column(name: str) -> FloatArray:
            return np.array([float(getattr(c, name)) for c in candles], dtype=np.float64)

        return cls(column("open"), column("high"), column("low"), column("close"), column("volume"))

    def __len__(self) -> int:
        return len(self.close)
