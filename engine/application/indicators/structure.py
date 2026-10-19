import numpy as np
import numpy.typing as npt

from engine.application.indicators.arrays import FloatArray

BoolArray = npt.NDArray[np.bool_]


def _pivots(values: FloatArray, left: int, right: int, *, high: bool) -> BoolArray:
    if left < 1 or right < 1:
        raise ValueError("left and right must be >= 1")
    sign = 1.0 if high else -1.0
    series = values * sign
    mask = np.zeros(len(series), dtype=np.bool_)
    # empate a direita conta (primeiro topo do patamar); so confirma com as `right` velas seguintes fechadas
    for j in range(left, len(series) - right):
        if series[j] > series[j - left : j].max() and series[j] >= series[j + 1 : j + right + 1].max():
            mask[j] = True
    return mask


def swing_highs(high: FloatArray, left: int = 3, right: int = 3) -> BoolArray:
    return _pivots(high, left, right, high=True)


def swing_lows(low: FloatArray, left: int = 3, right: int = 3) -> BoolArray:
    return _pivots(low, left, right, high=False)
