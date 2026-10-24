from typing import NamedTuple

import numpy as np
import numpy.typing as npt
from numpy.lib.stride_tricks import sliding_window_view

from engine.application.indicators.arrays import FloatArray
from engine.application.indicators.core import _check_period, _empty, atr, ema

IntArray = npt.NDArray[np.int64]


class Macd(NamedTuple):
    macd: FloatArray
    signal: FloatArray
    histogram: FloatArray


class Donchian(NamedTuple):
    upper: FloatArray
    middle: FloatArray
    lower: FloatArray


class Supertrend(NamedTuple):
    line: FloatArray
    direction: FloatArray


def macd(close: FloatArray, fast: int = 12, slow: int = 26, signal_period: int = 9) -> Macd:
    """MACD: EMA(fast) - EMA(slow); sinal = EMA da linha, semeada pela SMA dos primeiros valores validos."""
    if not fast < slow:
        raise ValueError("fast must be < slow")
    line = ema(close, fast) - ema(close, slow)
    signal = _empty(len(close))
    valid = line[slow - 1 :]
    if len(valid) >= signal_period:
        signal[slow - 1 :] = ema(valid, signal_period)
    return Macd(line, signal, line - signal)


def donchian(high: FloatArray, low: FloatArray, period: int = 20) -> Donchian:
    """Canal de maxima/minima das ultimas `period` velas, INCLUINDO a atual (rompimento: use o indice -2)."""
    _check_period(period)
    upper, lower = _empty(len(high)), _empty(len(high))
    if len(high) >= period:
        upper[period - 1 :] = sliding_window_view(high, period).max(axis=1)
        lower[period - 1 :] = sliding_window_view(low, period).min(axis=1)
    return Donchian(upper, (upper + lower) / 2.0, lower)


def supertrend(
    high: FloatArray, low: FloatArray, close: FloatArray, period: int = 10, multiple: float = 3.0
) -> Supertrend:
    """Supertrend sobre o ATR de Wilder. `direction` e +1 (alta), -1 (baixa) ou NaN no aquecimento.

    A direcao inicial (primeiro ATR valido) e +1 se o fechamento >= ponto medio da vela, senao -1.
    """
    size = len(close)
    line, direction = _empty(size), _empty(size)
    atr_values = atr(high, low, close, period)
    start = period
    if size <= start:
        return Supertrend(line, direction)
    mid = (high + low) / 2.0
    upper = mid + multiple * atr_values
    lower = mid - multiple * atr_values
    trend = 1.0 if close[start] >= mid[start] else -1.0
    final_upper, final_lower = upper[start], lower[start]
    line[start], direction[start] = (final_lower if trend > 0 else final_upper), trend
    for i in range(start + 1, size):
        final_upper = upper[i] if upper[i] < final_upper or close[i - 1] > final_upper else final_upper
        final_lower = lower[i] if lower[i] > final_lower or close[i - 1] < final_lower else final_lower
        if trend < 0 and close[i] > final_upper:
            trend = 1.0
        elif trend > 0 and close[i] < final_lower:
            trend = -1.0
        line[i], direction[i] = (final_lower if trend > 0 else final_upper), trend
    return Supertrend(line, direction)


def session_vwap(
    high: FloatArray, low: FloatArray, close: FloatArray, volume: FloatArray, session: IntArray
) -> FloatArray:
    """VWAP acumulado por sessao (`session` = id inteiro da sessao por vela, ex.: ordinal da data UTC).

    Preco tipico = (H+L+C)/3. Sessao sem volume acumulado fica NaN.
    """
    out = _empty(len(close))
    typical = (high + low + close) / 3.0
    cum_pv = cum_v = 0.0
    previous: int | None = None
    for i in range(len(close)):
        if session[i] != previous:
            cum_pv = cum_v = 0.0
            previous = int(session[i])
        cum_pv += typical[i] * volume[i]
        cum_v += volume[i]
        if cum_v > 0:
            out[i] = cum_pv / cum_v
    return out


__all__ = ["Donchian", "Macd", "Supertrend", "donchian", "macd", "session_vwap", "supertrend"]
