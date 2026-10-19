import math
from collections.abc import Callable

import numpy as np
import pytest

from engine.application.indicators import (
    CandleArrays,
    FloatArray,
    adx,
    atr,
    bollinger,
    ema,
    rsi,
    sma,
    swing_highs,
    swing_lows,
    true_range,
    volume_average,
)
from tests.unit.application.helpers import build_candles


def arr(*values: float) -> FloatArray:
    return np.array(values, dtype=np.float64)


def test_sma_reference() -> None:
    out = sma(arr(1, 2, 3, 4, 5), 3)
    assert np.isnan(out[:2]).all()
    assert out[2:].tolist() == [2.0, 3.0, 4.0]


def test_sma_series_shorter_than_period_is_all_nan() -> None:
    assert np.isnan(sma(arr(1, 2), 3)).all()


def test_volume_average_is_sma() -> None:
    assert volume_average(arr(10, 20, 30), 2).tolist()[1:] == [15.0, 25.0]


def test_ema_seeded_by_sma() -> None:
    out = ema(arr(2, 4, 6, 8, 10), 3)
    assert np.isnan(out[:2]).all()
    assert out[2:].tolist() == [4.0, 6.0, 8.0]


def test_ema_of_constant_is_constant() -> None:
    assert ema(np.full(30, 7.0), 10)[9:].tolist() == [7.0] * 21


def test_true_range_uses_previous_close() -> None:
    high, low, close = arr(10, 12, 13, 12, 14), arr(8, 9, 11, 10, 11), arr(9, 11, 12, 11, 13)
    assert true_range(high, low, close).tolist() == [2.0, 3.0, 2.0, 2.0, 3.0]


def test_true_range_counts_gap() -> None:
    assert true_range(arr(10, 21), arr(9, 20), arr(10, 20.5)).tolist() == [1.0, 11.0]


def test_atr_wilder_by_hand() -> None:
    out = atr(arr(10, 12, 13, 12, 14), arr(8, 9, 11, 10, 11), arr(9, 11, 12, 11, 13), 3)
    assert np.isnan(out[:3]).all()
    assert out[3] == pytest.approx(7 / 3)
    assert out[4] == pytest.approx(23 / 9)


def test_adx_pure_uptrend_saturates() -> None:
    n = 12
    high = np.arange(n) + 10.0
    low = np.arange(n) + 9.0
    close = np.arange(n) + 9.5
    result = adx(high, low, close, 3)
    assert result.plus_di[3:].tolist() == pytest.approx([200 / 3] * (n - 3))
    assert result.minus_di[3:].tolist() == [0.0] * (n - 3)
    assert np.isnan(result.adx[:5]).all()
    assert result.adx[5:].tolist() == pytest.approx([100.0] * (n - 5))


def test_adx_pure_downtrend_has_dominant_minus_di() -> None:
    n = 12
    result = adx(20.0 - np.arange(n) + 1, 20.0 - np.arange(n), 20.0 - np.arange(n) + 0.5, 3)
    assert result.minus_di[-1] > result.plus_di[-1] == 0.0
    assert result.adx[-1] == pytest.approx(100.0)


def test_adx_flat_market_is_zero_not_nan() -> None:
    flat = np.full(20, 10.0)
    result = adx(flat, flat, flat, 3)
    assert result.adx[-1] == 0.0
    assert result.plus_di[-1] == 0.0


def _reference_adx(high: list[float], low: list[float], close: list[float], n: int) -> list[float]:
    """Implementacao independente: medias de Wilder (em vez de somas) e laco puro Python."""
    size = len(close)
    tr, pdm, mdm = [], [], []
    for i in range(1, size):
        up, down = high[i] - high[i - 1], low[i - 1] - low[i]
        tr.append(max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1])))
        pdm.append(up if up > down and up > 0 else 0.0)
        mdm.append(down if down > up and down > 0 else 0.0)

    def avg(xs: list[float]) -> list[float]:
        out = [sum(xs[:n]) / n]
        for x in xs[n:]:
            out.append((out[-1] * (n - 1) + x) / n)
        return out

    avg_tr, p, m = avg(tr), avg(pdm), avg(mdm)
    dx = [100 * abs(p[i] - m[i]) / (p[i] + m[i]) for i in range(len(avg_tr))]
    return avg(dx)


def test_adx_matches_independent_reference_on_random_walk() -> None:
    rng = np.random.default_rng(7)
    close = 100 + np.cumsum(rng.normal(0, 1, 120))
    high = close + rng.uniform(0.1, 1.0, 120)
    low = close - rng.uniform(0.1, 1.0, 120)
    ours = adx(high, low, close, 14).adx
    expected = _reference_adx(high.tolist(), low.tolist(), close.tolist(), 14)
    assert ours[~np.isnan(ours)].tolist() == pytest.approx(expected)


def test_rsi_matches_wilder_textbook_values() -> None:
    close = arr(
        44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08,
        45.89, 46.03, 45.61, 46.28, 46.28, 46.00, 46.03, 46.41, 46.22, 45.64,
    )  # fmt: skip
    out = rsi(close, 14)
    assert np.isnan(out[:14]).all()
    assert out[14:].tolist() == pytest.approx(
        [70.53, 66.32, 66.55, 69.41, 66.36, 57.97], abs=0.1
    )  # planilha de referencia arredonda as medias


def test_rsi_extremes() -> None:
    rising = np.arange(1.0, 30.0)
    assert rsi(rising, 14)[-1] == 100.0
    assert rsi(rising[::-1].copy(), 14)[-1] == pytest.approx(0.0)
    assert rsi(np.full(30, 5.0), 14)[-1] == 50.0


def test_bollinger_by_hand() -> None:
    bands = bollinger(arr(1, 2, 3, 4, 5), 3, 2.0)
    std = math.sqrt(2 / 3)
    assert bands.middle[2:].tolist() == [2.0, 3.0, 4.0]
    assert bands.upper[2] == pytest.approx(2 + 2 * std)
    assert bands.lower[4] == pytest.approx(4 - 2 * std)
    assert np.isnan(bands.upper[:2]).all()


def test_bollinger_constant_series_has_zero_width() -> None:
    bands = bollinger(np.full(10, 3.0), 5)
    assert bands.upper[-1] == bands.lower[-1] == 3.0


def test_swing_points_need_confirmation() -> None:
    highs = arr(1, 2, 5, 2, 1, 2, 3, 2, 1)
    assert np.flatnonzero(swing_highs(highs, 2, 2)).tolist() == [2, 6]
    assert not swing_highs(highs[:6], 2, 2)[5]
    assert np.flatnonzero(swing_lows(-highs, 2, 2)).tolist() == [2, 6]


def test_swing_requires_positive_windows() -> None:
    with pytest.raises(ValueError, match="left and right"):
        swing_highs(arr(1, 2, 3), 0, 1)


def test_period_must_be_positive() -> None:
    with pytest.raises(ValueError, match="period"):
        sma(arr(1, 2), 0)


@pytest.mark.parametrize("cut", [30, 55, 90])
def test_indicators_are_causal(cut: int) -> None:
    """Valores ate `cut` nao mudam quando as velas seguintes sao alteradas."""
    rng = np.random.default_rng(3)
    close = 100 + np.cumsum(rng.normal(0, 1, 120))
    altered = close.copy()
    altered[cut:] += 50
    functions: dict[str, Callable[[FloatArray], FloatArray]] = {
        "sma": lambda c: sma(c, 10),
        "ema": lambda c: ema(c, 10),
        "atr": lambda c: atr(c + 0.5, c - 0.5, c, 14),
        "adx": lambda c: adx(c + 0.5, c - 0.5, c, 14).adx,
        "rsi": lambda c: rsi(c, 14),
        "bollinger": lambda c: bollinger(c, 20).upper,
    }
    for name, fn in functions.items():
        assert np.allclose(fn(altered)[:cut], fn(close[:cut]), equal_nan=True), name


def test_candle_arrays_conversion() -> None:
    data = CandleArrays.from_candles(build_candles([(1, 2, 0.5, 1.5, 10), (1.5, 3, 1, 2, 20)]))
    assert len(data) == 2
    assert data.close.tolist() == [1.5, 2.0]
    assert data.volume.tolist() == [10.0, 20.0]
