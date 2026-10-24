import math

import numpy as np
import pytest

from engine.application.indicators import (
    FloatArray,
    donchian,
    macd,
    session_vwap,
    supertrend,
)


def arr(*values: float) -> FloatArray:
    return np.array(values, dtype=np.float64)


def walk(size: int = 120) -> tuple[FloatArray, FloatArray, FloatArray, FloatArray]:
    close = [100.0]
    for i in range(1, size):
        step = 1.3 * math.sin(i / 3.0) + 0.4 * math.cos(i / 1.7) + (0.15 if i > 70 else -0.1)
        close.append(close[-1] + step)
    c = np.array(close)
    wiggle = 0.5 + 0.3 * np.abs(np.sin(np.arange(size)))
    return c, c + wiggle, c - wiggle, np.array([100.0 + (i % 7) * 10 for i in range(size)])


def ema_list(values: list[float], period: int) -> list[float]:
    out = [sum(values[:period]) / period]
    for v in values[period:]:
        out.append(out[-1] + (v - out[-1]) * 2 / (period + 1))
    return out


class TestMacd:
    def test_matches_independent_reference(self) -> None:
        close = walk()[0]
        fast, slow = ema_list(list(close), 12), ema_list(list(close), 26)
        line = [f - s for f, s in zip(fast[14:], slow, strict=True)]
        signal = ema_list(line, 9)
        out = macd(close)
        assert out.macd[25:].tolist() == pytest.approx(line)
        assert out.signal[25 + 8 :].tolist() == pytest.approx(signal)
        assert np.isnan(out.signal[: 25 + 8]).all()
        expected_hist = [m - s for m, s in zip(line[8:], signal, strict=True)]
        assert out.histogram[33:].tolist() == pytest.approx(expected_hist)

    def test_constant_series_is_zero(self) -> None:
        out = macd(np.full(60, 5.0))
        assert out.macd[25:].tolist() == [0.0] * 35
        assert out.histogram[33:].tolist() == [0.0] * 27

    def test_by_hand_small_periods(self) -> None:
        out = macd(arr(1, 2, 3, 4, 5, 6), fast=2, slow=3, signal_period=2)
        ema2 = [1.5]
        for v in (3.0, 4.0, 5.0, 6.0):
            ema2.append(ema2[-1] + (v - ema2[-1]) * 2 / 3)
        ema3 = [2.0]
        for v in (4.0, 5.0, 6.0):
            ema3.append(ema3[-1] + (v - ema3[-1]) / 2)
        line = [a - b for a, b in zip(ema2[1:], ema3, strict=True)]
        assert out.macd[2:].tolist() == pytest.approx(line)
        assert out.signal[3] == pytest.approx((line[0] + line[1]) / 2)

    def test_requires_fast_below_slow(self) -> None:
        with pytest.raises(ValueError, match="fast"):
            macd(arr(1, 2, 3), fast=5, slow=5)


class TestDonchian:
    def test_matches_loop_reference(self) -> None:
        _, high, low, _ = walk()
        out = donchian(high, low, 20)
        for i in range(19, len(high)):
            assert out.upper[i] == max(high[i - 19 : i + 1])
            assert out.lower[i] == min(low[i - 19 : i + 1])
            assert out.middle[i] == (out.upper[i] + out.lower[i]) / 2
        assert np.isnan(out.upper[:19]).all()

    def test_by_hand(self) -> None:
        out = donchian(arr(5, 7, 6, 9), arr(3, 4, 2, 5), 2)
        assert out.upper[1:].tolist() == [7.0, 7.0, 9.0]
        assert out.lower[1:].tolist() == [3.0, 2.0, 2.0]


def supertrend_reference(
    high: list[float], low: list[float], close: list[float], period: int, mult: float
) -> list[tuple[float, float]]:
    tr = [high[0] - low[0]] + [
        max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))
        for i in range(1, len(close))
    ]
    atr_by_index = {period: sum(tr[1 : period + 1]) / period}
    for i in range(period + 1, len(close)):
        atr_by_index[i] = (atr_by_index[i - 1] * (period - 1) + tr[i]) / period
    bands: dict[int, tuple[float, float]] = {}
    result: list[tuple[float, float]] = []
    direction = 0.0
    for i in range(period, len(close)):
        hl2 = (high[i] + low[i]) / 2
        up, lo = hl2 + mult * atr_by_index[i], hl2 - mult * atr_by_index[i]
        if i == period:
            direction = 1.0 if close[i] >= hl2 else -1.0
        else:
            prev_up, prev_lo = bands[i - 1]
            up = up if (up < prev_up or close[i - 1] > prev_up) else prev_up
            lo = lo if (lo > prev_lo or close[i - 1] < prev_lo) else prev_lo
            if direction < 0 and close[i] > up:
                direction = 1.0
            elif direction > 0 and close[i] < lo:
                direction = -1.0
        bands[i] = (up, lo)
        result.append((lo if direction > 0 else up, direction))
    return result


class TestSupertrend:
    def test_matches_independent_reference(self) -> None:
        close, high, low, _ = walk()
        out = supertrend(high, low, close, 10, 3.0)
        expected = supertrend_reference(list(high), list(low), list(close), 10, 3.0)
        assert out.line[10:].tolist() == pytest.approx([e[0] for e in expected])
        assert out.direction[10:].tolist() == [e[1] for e in expected]
        assert np.isnan(out.direction[:10]).all()
        assert {1.0, -1.0} <= set(out.direction[10:].tolist())

    def test_uptrend_line_trails_below_price(self) -> None:
        close = np.linspace(100, 160, 60)
        out = supertrend(close + 0.5, close - 0.5, close, 10, 3.0)
        assert (out.direction[10:] == 1.0).all()
        assert (out.line[10:] < close[10:]).all()
        assert (np.diff(out.line[10:]) >= 0).all()

    def test_flip_to_up_when_close_crosses_upper_band(self) -> None:
        close = np.concatenate([np.linspace(100, 70, 40), np.linspace(70, 130, 40)])
        out = supertrend(close + 0.5, close - 0.5, close, 10, 3.0)
        assert out.direction[39] == -1.0
        assert out.direction[-1] == 1.0

    def test_short_series_is_all_nan(self) -> None:
        out = supertrend(arr(2, 3), arr(1, 2), arr(1.5, 2.5), 10)
        assert np.isnan(out.line).all()


class TestSessionVwap:
    def test_by_hand_and_session_reset(self) -> None:
        high, low, close = arr(11, 12, 20, 22), arr(9, 10, 18, 18), arr(10, 11, 19, 20)
        volume, session = arr(1, 3, 2, 2), np.array([1, 1, 2, 2], dtype=np.int64)
        out = session_vwap(high, low, close, volume, session)
        assert out[0] == pytest.approx(10.0)
        assert out[1] == pytest.approx((10 * 1 + 11 * 3) / 4)
        assert out[2] == pytest.approx(19.0)
        assert out[3] == pytest.approx((19 * 2 + 20 * 2) / 4)

    def test_zero_volume_session_is_nan(self) -> None:
        out = session_vwap(arr(2, 2), arr(1, 1), arr(1.5, 1.5), arr(0, 5), np.array([1, 1], dtype=np.int64))
        assert np.isnan(out[0])
        assert out[1] == pytest.approx(1.5)


@pytest.mark.parametrize("cut", [30, 55, 80, 119])
def test_overlays_are_causal(cut: int) -> None:
    close, high, low, volume = walk()
    session = (np.arange(len(close)) // 24).astype(np.int64)
    full = (
        macd(close).histogram,
        donchian(high, low, 20).upper,
        supertrend(high, low, close, 10).line,
        session_vwap(high, low, close, volume, session),
    )
    prefix = (
        macd(close[:cut]).histogram,
        donchian(high[:cut], low[:cut], 20).upper,
        supertrend(high[:cut], low[:cut], close[:cut], 10).line,
        session_vwap(high[:cut], low[:cut], close[:cut], volume[:cut], session[:cut]),
    )
    for whole, part in zip(full, prefix, strict=True):
        np.testing.assert_allclose(whole[:cut], part, equal_nan=True)
