import itertools
import math
import random
from collections import Counter

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from engine.application.regime import (
    Regime,
    RegimeConfig,
    RegimeTimeline,
    classify_regime,
    classify_series,
)
from engine.domain.candle import Candle
from engine.domain.types import Timeframe
from tests.unit.application.helpers import T0, Row, build_candles

N = 300


def rows_from(closes: list[float], volumes: list[float] | None = None, wick: float = 0.3) -> list[Row]:
    volumes = volumes or [1000.0] * len(closes)
    rows: list[Row] = []
    previous = closes[0]
    for close, volume in zip(closes, volumes, strict=True):
        rows.append((previous, max(previous, close) + wick, min(previous, close) - wick, close, volume))
        previous = close
    return rows


def candles_from(closes: list[float], volumes: list[float] | None = None) -> list[Candle]:
    return build_candles(rows_from(closes, volumes), Timeframe.H1)


def mean_reverting(seed: int, phi: float = 0.8, amp: float = 1.0, n: int = 400) -> list[float]:
    rnd = random.Random(seed)  # noqa: S311
    x, out = 0.0, []
    for _ in range(n):
        x = phi * x + rnd.uniform(-amp, amp)
        out.append(100 + x)
    return out


def sine(n: int = N, amp: float = 5.0) -> list[float]:
    return [100 + amp * math.sin(2 * math.pi * i / 20) for i in range(n)]


def test_uptrend_is_classified_with_justifying_metrics() -> None:
    reading = classify_regime(candles_from([100 + 0.5 * i for i in range(N)]))
    assert reading.regime is Regime.TENDENCIA_ALTA
    assert reading.operable
    assert reading.metrics is not None
    assert reading.metrics.adx >= 25
    assert reading.metrics.plus_di > reading.metrics.minus_di
    assert "ADX" in reading.reason
    assert reading.to_context()["regime"] == "tendencia_alta"


def test_downtrend() -> None:
    reading = classify_regime(candles_from([400 - 0.5 * i for i in range(N)]))
    assert reading.regime is Regime.TENDENCIA_BAIXA
    assert reading.operable


def test_lateral_with_defined_range_is_operable() -> None:
    readings = classify_series(candles_from(mean_reverting(2)))
    lateral = [r for r in readings if r.regime is Regime.LATERAL and r.range_defined]
    assert lateral
    assert all(r.operable and r.metrics and r.metrics.range_touches_top for r in lateral)


def test_lateral_without_defined_range_is_not_operable() -> None:
    readings = classify_series(candles_from(mean_reverting(2, phi=0.95, amp=0.5)))
    undefined = [r for r in readings if r.regime is Regime.LATERAL and not r.range_defined]
    assert undefined
    assert not any(r.operable for r in undefined)
    assert "sem faixa" in undefined[0].reason


def test_high_volatility_means_no_operation() -> None:
    closes = sine()[:-15] + [100 + 25 * math.sin(2 * math.pi * i / 20) for i in range(15)]
    reading = classify_regime(candles_from(closes))
    assert reading.regime is Regime.ALTA_VOLATILIDADE
    assert not reading.operable


def test_low_liquidity_means_no_operation() -> None:
    reading = classify_regime(candles_from(sine(), [1000.0] * (N - 10) + [50.0] * 10))
    assert reading.regime is Regime.BAIXA_LIQUIDEZ
    assert not reading.operable


def test_warmup_is_undefined_and_not_operable() -> None:
    readings = classify_series(candles_from([100 + 0.5 * i for i in range(N)]))
    cfg = RegimeConfig()
    assert all(r.regime is Regime.INDEFINIDO and r.metrics is None for r in readings[: cfg.min_history - 1])
    assert readings[cfg.min_history].metrics is not None
    assert not readings[0].operable


def test_open_candle_is_rejected() -> None:
    candles = candles_from([100 + 0.5 * i for i in range(N)])
    candles[-1] = candles[-1].model_copy(update={"closed": False})
    with pytest.raises(ValueError, match="closed"):
        classify_series(candles)


def test_config_rejects_overlapping_thresholds() -> None:
    with pytest.raises(ValueError, match="adx_lateral_max"):
        RegimeConfig(adx_lateral_max=30, adx_trend_min=25)


def _changes(readings: list[Regime]) -> int:
    return sum(1 for a, b in itertools.pairwise(readings) if a != b)


def test_hysteresis_reduces_flicker_and_requires_confirmation() -> None:
    cfg = RegimeConfig()
    candles = candles_from(mean_reverting(1, phi=0.9, amp=1.0))
    readings = classify_series(candles)
    assert _changes([r.regime for r in readings]) < _changes([r.raw_regime for r in readings])
    for previous, current, index in zip(readings, readings[1:], range(1, len(readings)), strict=False):
        if current.regime is previous.regime or previous.metrics is None:
            continue
        if current.regime in (Regime.ALTA_VOLATILIDADE, Regime.BAIXA_LIQUIDEZ):
            assert current.raw_regime is current.regime
            continue
        window = readings[index - cfg.confirm_bars + 1 : index + 1]
        assert all(r.raw_regime is current.regime for r in window)


def test_single_noisy_candle_does_not_flip_confirmed_regime() -> None:
    closes = [100 + 0.5 * i for i in range(N)]
    clean = classify_series(candles_from(closes))
    noisy = closes[:]
    noisy[200] -= 6.0
    after = classify_series(candles_from(noisy))
    assert [r.regime for r in after[210:]] == [r.regime for r in clean[210:]]


def test_prefix_causality_and_future_independence() -> None:
    closes = mean_reverting(3, phi=0.9) + [100 + 0.6 * i for i in range(150)]
    candles = candles_from(closes)
    full = classify_series(candles)
    for cut in (130, 200, 333, 450):
        assert classify_series(candles[:cut]) == full[:cut]
    altered = candles_from(closes[:300] + [c * 1.7 for c in closes[300:]])
    assert classify_series(altered)[:300] == full[:300]


@given(
    seed=st.integers(0, 10_000),
    phi=st.floats(0.0, 0.98),
    cut=st.integers(120, 249),
)
@settings(max_examples=15, deadline=None)
def test_prefix_property_random_series(seed: int, phi: float, cut: int) -> None:
    candles = candles_from(mean_reverting(seed, phi=phi, amp=1.5, n=250))
    assert classify_series(candles[:cut]) == classify_series(candles)[:cut]


def test_timeline_returns_last_closed_candle_reading() -> None:
    candles = candles_from([100 + 0.5 * i for i in range(N)])
    timeline = RegimeTimeline.from_candles(candles)
    assert timeline.at(T0) is None
    reading = timeline.at(candles[250].close_time)
    assert reading is not None and reading.at == candles[250].close_time
    inside = timeline.at(candles[250].close_time + (candles[251].close_time - candles[250].close_time) / 2)
    assert inside == reading
    assert Counter(r.regime for r in classify_series(candles))[Regime.TENDENCIA_ALTA] > 100
