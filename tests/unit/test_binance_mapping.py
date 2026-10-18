from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from engine.domain.types import Timeframe
from engine.infrastructure.binance.backoff import ExponentialBackoff
from engine.infrastructure.binance.mapping import parse_depth, parse_rest_kline, parse_ws_kline

NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)


def ws_message(*, closed: bool) -> dict[str, Any]:
    return {
        "e": "kline",
        "s": "BTCUSDT",
        "k": {
            "t": 1767225600000,
            "T": 1767225659999,
            "o": "100.10",
            "h": "101.00",
            "l": "99.50",
            "c": "100.55",
            "v": "12.345678",
            "x": closed,
        },
    }


def test_ws_open_kline_is_ignored() -> None:
    assert parse_ws_kline("BTC/USDT", Timeframe.M1, ws_message(closed=False)) is None


def test_ws_closed_kline_marked_closed_with_exact_decimals() -> None:
    candle = parse_ws_kline("BTC/USDT", Timeframe.M1, ws_message(closed=True))
    assert candle is not None
    assert candle.closed
    assert candle.open_time == datetime(2026, 1, 1, tzinfo=UTC)
    assert candle.volume == Decimal("12.345678")


def test_ws_non_kline_event_ignored() -> None:
    assert parse_ws_kline("BTC/USDT", Timeframe.M1, {"e": "trade"}) is None


@pytest.mark.parametrize(("close_ms", "expected"), [(1767268799999, True), (1767312000000, False)])
def test_rest_kline_closed_flag_depends_on_close_time(close_ms: int, expected: bool) -> None:
    row = [1767225600000, "1", "2", "1", "1.5", "10", close_ms]
    assert parse_rest_kline("BTC/USDT", Timeframe.H1, row, NOW).closed is expected


def test_depth_skips_zero_quantity_levels() -> None:
    book = parse_depth("BTC/USDT", {"bids": [["100", "1"], ["99", "0"]], "asks": [["101", "2"]]}, NOW)
    assert [lvl.price for lvl in book.bids] == [Decimal(100)]
    assert book.asks[0].quantity == 2


def test_backoff_grows_is_capped_and_resets() -> None:
    backoff = ExponentialBackoff(base=1, cap=8)
    delays = [backoff.next_delay() for _ in range(6)]
    assert all(d <= 8 for d in delays)
    assert delays[0] <= 1
    assert delays[5] >= 4
    backoff.reset()
    assert backoff.next_delay() <= 1
