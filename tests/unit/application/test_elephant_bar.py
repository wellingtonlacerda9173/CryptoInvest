import pytest
from pydantic import ValidationError

from engine.application.detectors import (
    ElephantBarConfig,
    ElephantContext,
    ElephantDirection,
    detect_elephant_bar,
)
from tests.unit.application.helpers import build_candles, candle_row, wave_rows

BASE_COUNT = 70


def series(last: tuple[float, float, float, float, float] | None, count: int = BASE_COUNT):
    rows, _ = wave_rows(count)
    if last is not None:
        rows.append(last)
    return build_candles(rows)


def bullish(volume: float = 300.0):
    return candle_row(100.0, 102.5, volume, 0.1)


def test_bullish_elephant_detected_with_context() -> None:
    result = detect_elephant_bar(series(bullish()))
    assert result is not None
    assert result.direction is ElephantDirection.BULLISH
    assert ElephantContext.STRUCTURE_BREAK in result.contexts
    assert ElephantContext.TREND_IGNITION in result.contexts
    assert ElephantContext.EXHAUSTION not in result.contexts
    assert result.index == BASE_COUNT
    assert result.body_to_range >= 0.7
    assert result.volume_ratio >= 1.5
    assert 0 <= result.confidence <= 1


def test_bearish_elephant_breaks_support() -> None:
    result = detect_elephant_bar(series(candle_row(100.0, 97.5, 300.0, 0.1)))
    assert result is not None
    assert result.direction is ElephantDirection.BEARISH
    assert ElephantContext.STRUCTURE_BREAK in result.contexts


def test_small_body_is_not_elephant() -> None:
    assert detect_elephant_bar(series(candle_row(100.0, 100.6, 300.0, 0.05))) is None


def test_low_volume_blocks_detection() -> None:
    assert detect_elephant_bar(series(bullish(volume=120.0))) is None


def test_long_wicks_fail_body_ratio() -> None:
    assert detect_elephant_bar(series((100.0, 106.0, 98.0, 102.5, 300.0))) is None


def test_volume_threshold_is_editable() -> None:
    candles = series(bullish(volume=140.0))
    assert detect_elephant_bar(candles) is None
    assert detect_elephant_bar(candles, ElephantBarConfig(volume_multiple=1.2)) is not None


def test_atr_branch_is_an_alternative_to_average_body() -> None:
    candles = series(bullish())
    only_atr = ElephantBarConfig(body_avg_multiple=100.0)
    assert detect_elephant_bar(candles, only_atr) is not None
    assert detect_elephant_bar(candles, only_atr.model_copy(update={"use_atr_criterion": False})) is None


def test_confidence_grows_with_volume() -> None:
    weak = detect_elephant_bar(series(bullish(volume=160.0)))
    strong = detect_elephant_bar(series(bullish(volume=400.0)))
    assert weak is not None
    assert strong is not None
    assert strong.confidence > weak.confidence


def test_exhaustion_after_long_run_lowers_confidence() -> None:
    rows, _ = wave_rows(BASE_COUNT)
    price = rows[-1][3]
    fresh = detect_elephant_bar(build_candles([*rows, candle_row(price, price + 2.5, 300.0, 0.1)]))
    for _ in range(10):
        rows.append(candle_row(price, price + 0.9, 105.0, 0.1))
        price += 0.9
    tired = detect_elephant_bar(build_candles([*rows, candle_row(price, price + 2.5, 300.0, 0.1)]))
    assert fresh is not None
    assert tired is not None
    assert ElephantContext.EXHAUSTION in tired.contexts
    assert tired.confidence < fresh.confidence


def test_requires_enough_history() -> None:
    assert detect_elephant_bar(series(bullish(), count=10)) is None


def test_open_candle_is_ignored() -> None:
    candles = series(bullish())
    candles[-1] = candles[-1].model_copy(update={"closed": False})
    assert detect_elephant_bar(candles) is None


def test_zero_range_candle_is_none() -> None:
    assert detect_elephant_bar(series((100.0, 100.0, 100.0, 100.0, 300.0))) is None


def test_index_out_of_range_raises() -> None:
    with pytest.raises(IndexError):
        detect_elephant_bar(series(bullish()), index=999)


def test_future_candles_do_not_change_result() -> None:
    """Sem lookahead: alterar ou acrescentar velas depois do indice nao muda o resultado."""
    base = series(bullish())
    idx = len(base) - 1
    expected = detect_elephant_bar(base, index=idx)
    future = build_candles([candle_row(500.0, 900.0, 9999.0)] * 5, start=base[-1].close_time)
    altered = [*base, *future]
    assert detect_elephant_bar(altered, index=idx) == expected
    assert detect_elephant_bar(base) == expected


def test_scanning_prefixes_matches_indexed_detection() -> None:
    candles = series(bullish())
    for i in range(len(candles)):
        assert detect_elephant_bar(candles[: i + 1]) == detect_elephant_bar(candles, index=i)


@pytest.mark.parametrize(
    "overrides",
    [{"min_body_ratio": 0.0}, {"min_body_ratio": 1.1}, {"lookback": 1}, {"volume_multiple": 0}],
)
def test_invalid_config_rejected(overrides: dict[str, float]) -> None:
    with pytest.raises(ValidationError):
        ElephantBarConfig(**overrides)
