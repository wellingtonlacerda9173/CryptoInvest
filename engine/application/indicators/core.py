from typing import NamedTuple

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from engine.application.indicators.arrays import FloatArray


class BollingerBands(NamedTuple):
    lower: FloatArray
    middle: FloatArray
    upper: FloatArray


class Adx(NamedTuple):
    adx: FloatArray
    plus_di: FloatArray
    minus_di: FloatArray


def _check_period(period: int) -> None:
    if period < 1:
        raise ValueError("period must be >= 1")


def _empty(size: int) -> FloatArray:
    return np.full(size, np.nan, dtype=np.float64)


def sma(values: FloatArray, period: int) -> FloatArray:
    _check_period(period)
    out = _empty(len(values))
    if len(values) >= period:
        out[period - 1 :] = sliding_window_view(values, period).mean(axis=1)
    return out


def ema(values: FloatArray, period: int) -> FloatArray:
    """EMA com alpha = 2/(n+1), semeada pela SMA das primeiras `period` velas."""
    _check_period(period)
    out = _empty(len(values))
    if len(values) < period:
        return out
    alpha = 2.0 / (period + 1)
    out[period - 1] = values[:period].mean()
    for i in range(period, len(values)):
        out[i] = out[i - 1] + alpha * (values[i] - out[i - 1])
    return out


def _wilder(values: FloatArray, period: int, first: int) -> FloatArray:
    """Suavizacao de Wilder: semente = media de values[first:first+period], depois (prev*(n-1)+x)/n."""
    out = _empty(len(values))
    seed = first + period - 1
    if len(values) <= seed:
        return out
    out[seed] = values[first : seed + 1].mean()
    for i in range(seed + 1, len(values)):
        out[i] = (out[i - 1] * (period - 1) + values[i]) / period
    return out


def true_range(high: FloatArray, low: FloatArray, close: FloatArray) -> FloatArray:
    tr = high - low
    if len(tr) > 1:
        prev_close = close[:-1]
        tr[1:] = np.maximum.reduce(
            [high[1:] - low[1:], np.abs(high[1:] - prev_close), np.abs(low[1:] - prev_close)]
        )
    return tr


def atr(high: FloatArray, low: FloatArray, close: FloatArray, period: int = 14) -> FloatArray:
    """ATR de Wilder; primeiro valor no indice `period` (a TR da vela 0 nao tem fechamento anterior)."""
    _check_period(period)
    return _wilder(true_range(high, low, close), period, first=1)


def _wilder_sum(values: FloatArray, period: int) -> FloatArray:
    out = _empty(len(values))
    if len(values) <= period:
        return out
    out[period] = values[1 : period + 1].sum()
    for i in range(period + 1, len(values)):
        out[i] = out[i - 1] - out[i - 1] / period + values[i]
    return out


def adx(high: FloatArray, low: FloatArray, close: FloatArray, period: int = 14) -> Adx:
    """ADX e DI+/DI- de Wilder. DI valido a partir do indice `period`, ADX a partir de `2*period - 1`."""
    _check_period(period)
    size = len(close)
    plus_dm = np.zeros(size)
    minus_dm = np.zeros(size)
    if size > 1:
        up = high[1:] - high[:-1]
        down = low[:-1] - low[1:]
        plus_dm[1:] = np.where((up > down) & (up > 0), up, 0.0)
        minus_dm[1:] = np.where((down > up) & (down > 0), down, 0.0)
    smoothed_tr = _wilder_sum(true_range(high, low, close), period)
    with np.errstate(divide="ignore", invalid="ignore"):
        plus_di = np.where(smoothed_tr > 0, 100.0 * _wilder_sum(plus_dm, period) / smoothed_tr, 0.0)
        minus_di = np.where(smoothed_tr > 0, 100.0 * _wilder_sum(minus_dm, period) / smoothed_tr, 0.0)
        total = plus_di + minus_di
        dx = np.where(total > 0, 100.0 * np.abs(plus_di - minus_di) / total, 0.0)
    warmup = np.isnan(smoothed_tr)
    plus_di[warmup] = np.nan
    minus_di[warmup] = np.nan
    dx[warmup] = np.nan
    return Adx(_wilder(dx, period, first=period), plus_di, minus_di)


def rsi(close: FloatArray, period: int = 14) -> FloatArray:
    _check_period(period)
    gain = np.zeros(len(close))
    loss = np.zeros(len(close))
    if len(close) > 1:
        delta = np.diff(close)
        gain[1:] = np.maximum(delta, 0.0)
        loss[1:] = np.maximum(-delta, 0.0)
    avg_gain = _wilder(gain, period, first=1)
    avg_loss = _wilder(loss, period, first=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        value = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    value = np.where(avg_loss == 0, np.where(avg_gain == 0, 50.0, 100.0), value)
    return np.where(np.isnan(avg_gain), np.nan, value)


def bollinger(close: FloatArray, period: int = 20, std_multiple: float = 2.0) -> BollingerBands:
    """Bandas com desvio-padrao populacional (ddof=0), convencao de Bollinger."""
    _check_period(period)
    middle = sma(close, period)
    std = _empty(len(close))
    if len(close) >= period:
        std[period - 1 :] = sliding_window_view(close, period).std(axis=1)
    return BollingerBands(middle - std_multiple * std, middle, middle + std_multiple * std)


def volume_average(volume: FloatArray, period: int = 20) -> FloatArray:
    return sma(volume, period)
