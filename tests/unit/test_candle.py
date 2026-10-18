from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from engine.domain.candle import Candle
from engine.domain.types import Timeframe

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def make(**overrides: object) -> Candle:
    data: dict[str, object] = {
        "symbol": "BTC/USDT",
        "timeframe": Timeframe.M1,
        "open_time": T0,
        "open": Decimal(100),
        "high": Decimal(110),
        "low": Decimal(90),
        "close": Decimal(105),
        "volume": Decimal(3),
        "closed": True,
    }
    return Candle(**(data | overrides))  # type: ignore[arg-type]


def test_valid_candle_is_frozen() -> None:
    candle = make()
    with pytest.raises(ValidationError):
        candle.close = Decimal(1)  # type: ignore[misc]


def test_high_below_close_rejected() -> None:
    with pytest.raises(ValidationError, match="high"):
        make(high=Decimal(104))


def test_low_above_open_rejected() -> None:
    with pytest.raises(ValidationError, match="low"):
        make(low=Decimal(101))


def test_negative_volume_rejected() -> None:
    with pytest.raises(ValidationError):
        make(volume=Decimal(-1))


def test_naive_timestamp_rejected() -> None:
    with pytest.raises(ValidationError):
        make(open_time=datetime(2026, 1, 1))


def test_timestamp_normalized_to_utc() -> None:
    brt = timezone(timedelta(hours=-3))
    candle = make(open_time=datetime(2025, 12, 31, 21, tzinfo=brt))
    assert candle.open_time == T0
    assert candle.open_time.utcoffset() == timedelta(0)


def test_zero_volume_allowed() -> None:
    assert make(volume=Decimal(0)).volume == 0


def test_close_time() -> None:
    assert make().close_time == T0 + timedelta(minutes=1)


prices = st.decimals(min_value=Decimal("0.01"), max_value=Decimal(100000), places=2)


@given(o=prices, h=prices, low=prices, c=prices)
def test_ohlc_invariant_holds_for_any_accepted_candle(
    o: Decimal, h: Decimal, low: Decimal, c: Decimal
) -> None:
    try:
        candle = make(open=o, high=h, low=low, close=c)
    except ValidationError:
        assert h < max(o, c) or low > min(o, c)
    else:
        assert candle.high >= max(candle.open, candle.close)
        assert candle.low <= min(candle.open, candle.close)
