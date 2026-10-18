import copy
import pickle
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import ccxt.async_support as ccxt
import pytest
from pydantic import ValidationError
from structlog.testing import capture_logs

from engine.domain.candle import Candle
from engine.domain.decision import Decision, DecisionAction
from engine.domain.order import EntryOrder, ExitOrder, OrderType, revalidate_order
from engine.domain.position import Position
from engine.domain.signal import Signal
from engine.domain.types import Timeframe
from engine.infrastructure.binance import market_data
from tests.unit.test_binance_rest import FrozenDatetime
from tests.unit.test_binance_stream import (  # noqa: F401  (fixture autouse + helpers)
    SYMBOL,
    T0,
    FakeSession,
    candle,
    fake_network,
    kline,
    make_client,
    take,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def valid_order() -> EntryOrder:
    return EntryOrder(
        client_order_id="abc",
        symbol="BTC/USDT",
        type=OrderType.LIMIT,
        quantity=Decimal(1),
        price=Decimal(100),
        stop_loss=Decimal(90),
        take_profit=Decimal(120),
        created_at=NOW,
    )


def valid_signal() -> Signal:
    return Signal(
        symbol="BTC/USDT",
        timeframe=Timeframe.H1,
        created_at=NOW,
        strategy="s",
        entry=Decimal(100),
        stop_loss=Decimal(90),
        take_profit=Decimal(120),
    )


def tampered_signal() -> Signal:
    signal = valid_signal()
    object.__setattr__(signal, "stop_loss", Decimal(150))
    return signal


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("stop_loss", Decimal(200)),
        ("stop_loss", Decimal(0)),
        ("take_profit", Decimal(101)),
        ("price", float("nan")),
        ("price", Decimal("NaN")),
    ],
)
def test_revalidate_order_rejects_order_tampered_with_object_setattr(field: str, value: object) -> None:
    order = valid_order()
    object.__setattr__(order, field, value)

    with pytest.raises(ValidationError):
        revalidate_order(order)


def test_revalidate_order_rejects_pickle_state_with_tampered_stop() -> None:
    state = valid_order().__getstate__()
    state["__dict__"]["stop_loss"] = Decimal(500)
    order = EntryOrder.__new__(EntryOrder)
    order.__setstate__(state)

    with pytest.raises(ValidationError):
        revalidate_order(order)


def test_revalidate_order_accepts_valid_orders_and_keeps_their_type() -> None:
    exit_order = ExitOrder(
        client_order_id="x", symbol="BTC/USDT", type=OrderType.MARKET, quantity=Decimal(1), created_at=NOW
    )

    assert revalidate_order(valid_order()) == valid_order()
    assert revalidate_order(exit_order) == exit_order


def test_decision_rejects_nested_signal_tampered_after_construction() -> None:
    with pytest.raises(ValidationError):
        Decision(
            decided_at=NOW,
            symbol="BTC/USDT",
            action=DecisionAction.ENTER,
            reason="r",
            source="s",
            signal=tampered_signal(),
        )


def test_decision_rejects_nested_signal_with_negative_stop_that_passes_cross_field_checks() -> None:
    signal = valid_signal()
    object.__setattr__(signal, "stop_loss", Decimal(-1000))
    object.__setattr__(signal, "take_profit", Decimal(10_000_000))

    with pytest.raises(ValidationError):
        Decision(
            decided_at=NOW,
            symbol="BTC/USDT",
            action=DecisionAction.ENTER,
            reason="r",
            source="s",
            signal=signal,
        )


def test_decision_model_copy_revalidates_nested_signal() -> None:
    hold = Decision(decided_at=NOW, symbol="BTC/USDT", action=DecisionAction.HOLD, reason="r", source="s")

    with pytest.raises(ValidationError):
        hold.model_copy(update={"signal": tampered_signal()})


def test_order_from_tampered_signal_is_rejected() -> None:
    with pytest.raises(ValidationError):
        EntryOrder.from_signal(tampered_signal(), client_order_id="a", quantity=Decimal(1))


def test_position_model_construct_cannot_put_stop_above_entry() -> None:
    with pytest.raises(ValidationError):
        Position.model_construct(
            symbol="BTC/USDT",
            quantity=Decimal(1),
            entry_price=Decimal(100),
            stop_loss=Decimal(101),
            opened_at=NOW,
        )


def test_candle_model_copy_cannot_break_ohlc() -> None:
    with pytest.raises(ValidationError):
        candle(0).model_copy(update={"high": Decimal("0.5")})


def test_model_dump_then_construct_cannot_bypass_levels() -> None:
    data = valid_order().model_dump()
    data["stop_loss"] = Decimal(500)

    with pytest.raises(ValidationError):
        EntryOrder.model_construct(**data)


@pytest.mark.parametrize("clone", [copy.copy, copy.deepcopy, lambda o: pickle.loads(pickle.dumps(o))])  # noqa: S301
def test_copies_of_valid_order_remain_equal_and_valid(clone: Any) -> None:
    order = valid_order()

    assert clone(order) == order


@pytest.mark.parametrize("price", ["100.5", "1e2"])
def test_json_floats_are_rejected_for_money(price: str) -> None:
    payload = (
        '{"client_order_id":"a","symbol":"BTC/USDT","type":"limit","quantity":"1",'
        f'"price":{price},"stop_loss":"50","take_profit":"300","created_at":"2026-01-01T00:00:00Z"}}'
    )

    with pytest.raises(ValidationError):
        EntryOrder.model_validate_json(payload)


async def _recovery_delays(
    monkeypatch: pytest.MonkeyPatch, error: Exception, headers: dict[str, str]
) -> list[float]:
    FakeSession.scripts = [[kline(0)], [kline(3), "HANG"]]
    delays: list[float] = []

    async def record(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(market_data.asyncio, "sleep", record)
    attempts = 0

    def flaky(_: datetime) -> list[Candle]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise error
        return [candle(1), candle(2)]

    client, _ = make_client(flaky)
    client._exchange.last_response_headers = headers  # type: ignore[assignment]

    await take(client.stream_candles(SYMBOL, Timeframe.M1), 4)
    return delays


async def test_recovery_waits_at_least_retry_after_on_418(monkeypatch: pytest.MonkeyPatch) -> None:
    delays = await _recovery_delays(monkeypatch, ccxt.DDoSProtection("418"), {"Retry-After": "120"})

    assert 120.0 in delays


async def test_recovery_waits_until_ban_expiry_on_418(monkeypatch: pytest.MonkeyPatch) -> None:
    ban_end_ms = int((datetime.now(UTC) + timedelta(seconds=300)).timestamp() * 1000)

    delays = await _recovery_delays(monkeypatch, ccxt.DDoSProtection(f"418 banned until {ban_end_ms}"), {})

    assert any(280 < d <= 300 for d in delays)


async def test_recovery_waits_at_least_retry_after_on_429(monkeypatch: pytest.MonkeyPatch) -> None:
    delays = await _recovery_delays(monkeypatch, ccxt.RateLimitExceeded("429"), {"Retry-After": "120"})

    assert 120.0 in delays


async def test_first_reconnect_without_since_recovers_with_one_candle_margin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeSession.scripts = [[], [kline(9), "HANG"]]
    FrozenDatetime.frozen = T0 + timedelta(minutes=5, seconds=30)
    monkeypatch.setattr(market_data, "datetime", FrozenDatetime)
    client, calls = make_client(lambda start: [])

    await take(client.stream_candles(SYMBOL, Timeframe.M1), 1)

    assert calls == [T0 + timedelta(minutes=4)]


async def test_exhausted_recovery_retries_log_the_gap_and_keep_streaming() -> None:
    FakeSession.scripts = [[kline(0)], [kline(3), "HANG"]]
    attempts = 0

    def always_down(_: datetime) -> list[Candle]:
        nonlocal attempts
        attempts += 1
        raise ccxt.RequestTimeout("timeout")

    client, _ = make_client(always_down)

    with capture_logs() as logs:
        got = await take(client.stream_candles(SYMBOL, Timeframe.M1), 2)

    # 5 tentativas na reconexao + 1 tentativa de backfill ao receber a vela ao vivo
    assert attempts == 6
    assert [c.open_time for c in got] == [T0, T0 + timedelta(minutes=3)]
    gap = [entry for entry in logs if entry["event"] == "binance_candle_gap"]
    assert len(gap) == 1
    assert gap[0]["from_open_time"] == (T0 + timedelta(minutes=1)).isoformat()


async def test_gap_left_by_exhausted_recovery_is_backfilled_later_while_ws_stays_up() -> None:
    FakeSession.scripts = [[kline(0)], [kline(3), kline(4), "HANG"]]
    calls = 0

    def down_then_up(_: datetime) -> list[Candle]:
        nonlocal calls
        calls += 1
        if calls <= 5:
            raise ccxt.RequestTimeout("timeout")
        return [candle(1), candle(2)]

    client, _ = make_client(down_then_up)

    got = await take(client.stream_candles(SYMBOL, Timeframe.M1), 5)

    assert [c.open_time for c in got] == [T0 + timedelta(minutes=i) for i in range(5)]


async def test_stream_survives_malformed_rest_row_during_recovery() -> None:
    FakeSession.scripts = [[kline(0)], [kline(3), "HANG"]]

    def malformed(_: datetime) -> list[Candle]:
        raise ValueError("linha de klines malformada")

    client, _ = make_client(malformed)

    got = await take(client.stream_candles(SYMBOL, Timeframe.M1), 2)

    assert got[-1].open_time == T0 + timedelta(minutes=3)


async def test_pending_gap_is_retried_on_every_live_candle_until_abandoned_in_order() -> None:
    FakeSession.scripts = [[kline(0)], [kline(3), kline(4), "HANG"]]

    def always_malformed(_: datetime) -> list[Candle]:
        raise KeyError("k")

    client, calls = make_client(always_malformed)

    with capture_logs() as logs:
        got = await take(client.stream_candles(SYMBOL, Timeframe.M1), 3)

    assert [c.open_time for c in got] == [T0, T0 + timedelta(minutes=3), T0 + timedelta(minutes=4)]
    assert len(calls) == 2
    assert len([e for e in logs if e["event"] == "binance_candle_gap"]) == 1
    assert any(e["event"] == "binance_candle_gap_abandoned" for e in logs)


async def test_backfill_on_live_candle_keeps_order_and_skips_duplicates() -> None:
    FakeSession.scripts = [[kline(0)], [kline(3), kline(4), "HANG"]]
    attempts = 0

    def down_once_then_overlapping(_: datetime) -> list[Candle]:
        nonlocal attempts
        attempts += 1
        if attempts <= 5:
            raise ccxt.RequestTimeout("timeout")
        return [candle(1), candle(2), candle(3)]

    client, _ = make_client(down_once_then_overlapping)

    got = await take(client.stream_candles(SYMBOL, Timeframe.M1), 5)

    assert [c.open_time for c in got] == [T0 + timedelta(minutes=i) for i in range(5)]


@pytest.mark.parametrize(
    "error", [ccxt.BadSymbol("binance does not have market symbol"), ccxt.BadRequest("400")]
)
async def test_permanent_rest_errors_fail_explicitly_without_retry(error: Exception) -> None:
    FakeSession.scripts = [[kline(0)], [kline(3), "HANG"]]
    attempts = 0

    def permanent(_: datetime) -> list[Candle]:
        nonlocal attempts
        attempts += 1
        raise error

    client, _ = make_client(permanent)

    with pytest.raises(market_data.MarketDataConfigError):
        await take(client.stream_candles(SYMBOL, Timeframe.M1), 3)
    assert attempts == 1


async def test_transient_exchange_error_is_retried_as_gap() -> None:
    FakeSession.scripts = [[kline(0)], [kline(3), "HANG"]]

    def odd(_: datetime) -> list[Candle]:
        raise ccxt.ExchangeError("binance unexpected payload")

    client, _ = make_client(odd)

    got = await take(client.stream_candles(SYMBOL, Timeframe.M1), 2)

    assert got[-1].open_time == T0 + timedelta(minutes=3)


@pytest.mark.parametrize(
    "message",
    [
        "binance GET https://api.binance.com/api/v3/klines?limit=451 503 Service Unavailable",
        'binance GET https://x 503 Service Unavailable {"msg":"order 451 not found"}',
        "binance GET https://x/451/klines 502 Bad Gateway",
    ],
)
async def test_451_substring_in_other_context_is_not_region_block(message: str) -> None:
    FakeSession.scripts = [[kline(0)], [kline(3), "HANG"]]
    attempts = 0

    def unavailable(_: datetime) -> list[Candle]:
        nonlocal attempts
        attempts += 1
        raise ccxt.ExchangeNotAvailable(message)

    client, _ = make_client(unavailable)

    got = await take(client.stream_candles(SYMBOL, Timeframe.M1), 2)

    assert got[-1].open_time == T0 + timedelta(minutes=3)
    assert attempts >= 5
