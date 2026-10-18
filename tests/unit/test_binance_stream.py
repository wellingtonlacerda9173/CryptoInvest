import asyncio
import json
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import TracebackType
from typing import Any, ClassVar, Self

import aiohttp
import ccxt.async_support as ccxt
import pytest

from engine.domain.candle import Candle
from engine.domain.types import Timeframe
from engine.infrastructure.binance import market_data
from engine.infrastructure.binance.backoff import ExponentialBackoff
from engine.infrastructure.binance.market_data import BinanceMarketData

SYMBOL = "BTC/USDT"
T0 = datetime(2026, 1, 1, tzinfo=UTC)
HANG = "HANG"
_real_sleep = asyncio.sleep


def kline(i: int, *, closed: bool = True) -> dict[str, Any]:
    start = T0 + timedelta(minutes=i)
    return {
        "stream": "btcusdt@kline_1m",
        "data": {
            "e": "kline",
            "k": {
                "t": int(start.timestamp() * 1000),
                "o": "100",
                "h": "101",
                "l": "99",
                "c": "100.5",
                "v": "1",
                "x": closed,
            },
        },
    }


def candle(i: int) -> Candle:
    return Candle(
        symbol=SYMBOL,
        timeframe=Timeframe.M1,
        open_time=T0 + timedelta(minutes=i),
        open=Decimal(100),
        high=Decimal(101),
        low=Decimal(99),
        close=Decimal("100.5"),
        volume=Decimal(1),
        closed=True,
    )


class FakeMsg:
    def __init__(self, payload: Any) -> None:
        self.type = aiohttp.WSMsgType.TEXT
        self.data = payload if isinstance(payload, str) else json.dumps(payload)


class FakeWS:
    def __init__(self, script: list[Any]) -> None:
        self._script = script

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    def __aiter__(self) -> AsyncIterator[FakeMsg]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[FakeMsg]:
        for item in self._script:
            if item == HANG:
                await asyncio.Event().wait()
            yield FakeMsg(item)


class FakeSession:
    """Fake de aiohttp.ClientSession. Cada conexao consome um roteiro (lista de mensagens, ou excecao)."""

    scripts: ClassVar[list[Any]] = []
    instances: ClassVar[list["FakeSession"]] = []

    def __init__(self, *_: Any, **__: Any) -> None:
        self.closed = False
        self.connects = 0
        FakeSession.instances.append(self)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.closed = True

    def ws_connect(self, *_: Any, **__: Any) -> FakeWS:
        self.connects += 1
        if not FakeSession.scripts:
            return FakeWS([HANG])
        script = FakeSession.scripts.pop(0)
        if isinstance(script, BaseException):
            raise script
        return FakeWS(script)


@pytest.fixture(autouse=True)
def fake_network(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeSession.scripts = []
    FakeSession.instances = []
    monkeypatch.setattr(market_data.aiohttp, "ClientSession", FakeSession)

    async def fast_sleep(_: float) -> None:
        await _real_sleep(0)

    monkeypatch.setattr(market_data.asyncio, "sleep", fast_sleep)


def make_client(
    recover: Callable[[datetime], list[Candle]] | None = None,
) -> tuple[BinanceMarketData, list[datetime]]:
    client = BinanceMarketData()
    calls: list[datetime] = []

    async def fake_fetch(
        symbol: str, timeframe: Timeframe, start: datetime, end: datetime | None = None
    ) -> list[Candle]:
        calls.append(start)
        return recover(start) if recover else []

    client.fetch_candles = fake_fetch  # type: ignore[method-assign]
    return client, calls


async def take(stream: AsyncIterator[Candle], n: int) -> list[Candle]:
    out: list[Candle] = []

    async def run() -> None:
        async for c in stream:
            out.append(c)
            if len(out) == n:
                return

    await asyncio.wait_for(run(), timeout=5)
    return out


async def test_stream_emits_only_closed_candles() -> None:
    FakeSession.scripts = [[kline(0, closed=False), kline(0), kline(1, closed=False), kline(1), HANG]]
    client, _ = make_client()

    got = await take(client.stream_candles(SYMBOL, Timeframe.M1), 2)

    assert [c.open_time for c in got] == [T0, T0 + timedelta(minutes=1)]
    assert all(c.closed for c in got)


async def test_stream_drops_duplicates_and_out_of_order_candles() -> None:
    FakeSession.scripts = [[kline(1), kline(1), kline(0), kline(2), HANG]]
    client, _ = make_client()

    got = await take(client.stream_candles(SYMBOL, Timeframe.M1), 2)

    assert [c.open_time for c in got] == [T0 + timedelta(minutes=1), T0 + timedelta(minutes=2)]


async def test_reconnect_recovers_missed_candles_in_order_without_duplicates() -> None:
    FakeSession.scripts = [[kline(0)], [kline(3), kline(4), HANG]]
    client, calls = make_client(lambda start: [candle(1), candle(2), candle(3)])

    got = await take(client.stream_candles(SYMBOL, Timeframe.M1), 5)

    assert [c.open_time for c in got] == [T0 + timedelta(minutes=i) for i in range(5)]
    assert calls == [T0 + timedelta(minutes=1)]


async def test_reconnect_after_connect_errors_resumes() -> None:
    FakeSession.scripts = [OSError("boom"), aiohttp.ClientConnectionError("down"), [kline(0), HANG]]
    client, _ = make_client()

    got = await take(client.stream_candles(SYMBOL, Timeframe.M1), 1)

    assert got[0].open_time == T0
    assert FakeSession.instances[0].connects == 3


async def test_one_session_is_reused_across_reconnects() -> None:
    FakeSession.scripts = [[kline(0)], [kline(1)], [kline(2), HANG]]
    client, _ = make_client(lambda start: [])

    await take(client.stream_candles(SYMBOL, Timeframe.M1), 3)

    assert len(FakeSession.instances) == 1


async def test_cancellation_closes_session() -> None:
    FakeSession.scripts = [[kline(0), HANG]]
    client, _ = make_client()
    got: list[Candle] = []

    async def consume() -> None:
        async for c in client.stream_candles(SYMBOL, Timeframe.M1):
            got.append(c)

    task = asyncio.create_task(consume())
    await _real_sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(got) == 1
    assert FakeSession.instances[0].closed


async def test_aclose_of_stream_closes_session_deterministically() -> None:
    FakeSession.scripts = [[kline(0), kline(1), HANG]]
    client, _ = make_client()
    stream = client.stream_candles(SYMBOL, Timeframe.M1)

    await anext(stream)
    await stream.aclose()  # type: ignore[attr-defined]

    assert FakeSession.instances[0].closed


async def test_candle_closed_during_first_reconnect_is_recovered() -> None:
    FakeSession.scripts = [[kline(0, closed=False)], [kline(5), HANG]]
    client, calls = make_client(lambda start: [candle(i) for i in range(1, 5)])

    await take(client.stream_candles(SYMBOL, Timeframe.M1), 1)

    assert calls, "nenhuma recuperacao REST foi tentada apos a reconexao"


async def test_stream_survives_rest_failure_during_recovery() -> None:
    FakeSession.scripts = [[kline(0)], [kline(3), HANG]]

    def fail(_: datetime) -> list[Candle]:
        raise OSError("simula 429/418/451 do REST")

    client, _ = make_client(fail)

    got = await take(client.stream_candles(SYMBOL, Timeframe.M1), 2)

    assert got[-1].open_time == T0 + timedelta(minutes=3)


async def test_stream_ignores_unexpected_ws_payloads() -> None:
    FakeSession.scripts = [[{"result": None, "id": 1}, kline(0), HANG]]
    client, _ = make_client()

    got = await take(client.stream_candles(SYMBOL, Timeframe.M1), 1)

    assert got[0].open_time == T0


async def test_stream_skips_malformed_json() -> None:
    FakeSession.scripts = [["{not json", kline(0), HANG]]
    client, _ = make_client()

    got = await take(client.stream_candles(SYMBOL, Timeframe.M1), 1)

    assert got[0].open_time == T0


@pytest.mark.parametrize("error", [ccxt.RateLimitExceeded("429"), ccxt.DDoSProtection("418 banned until 1")])
async def test_recovery_retries_rate_limit_errors_then_succeeds(error: Exception) -> None:
    FakeSession.scripts = [[kline(0)], [kline(3), HANG]]
    attempts = 0

    def flaky(_: datetime) -> list[Candle]:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise error
        return [candle(1), candle(2)]

    client, _ = make_client(flaky)

    got = await take(client.stream_candles(SYMBOL, Timeframe.M1), 4)

    assert [c.open_time for c in got] == [T0 + timedelta(minutes=i) for i in (0, 1, 2, 3)]
    assert attempts == 3


async def test_recovery_451_fails_explicitly_without_retry() -> None:
    FakeSession.scripts = [[kline(0)], [kline(3), HANG]]
    attempts = 0

    def blocked(_: datetime) -> list[Candle]:
        nonlocal attempts
        attempts += 1
        raise ccxt.ExchangeNotAvailable("binance GET https://x 451 Unavailable For Legal Reasons")

    client, _ = make_client(blocked)

    with pytest.raises(market_data.RegionBlockedError):
        await take(client.stream_candles(SYMBOL, Timeframe.M1), 3)
    assert attempts == 1


async def test_since_recovers_history_on_first_connection() -> None:
    FakeSession.scripts = [[kline(4), HANG]]
    client, calls = make_client(lambda start: [candle(2), candle(3)])

    got = await take(client.stream_candles(SYMBOL, Timeframe.M1, since=T0 + timedelta(minutes=2)), 3)

    assert calls == [T0 + timedelta(minutes=2)]
    assert [c.open_time for c in got] == [T0 + timedelta(minutes=i) for i in (2, 3, 4)]


async def test_malformed_ohlc_message_is_discarded() -> None:
    bad = kline(0)
    bad["data"]["k"]["h"] = "1"
    FakeSession.scripts = [[bad, kline(1), HANG]]
    client, _ = make_client()

    got = await take(client.stream_candles(SYMBOL, Timeframe.M1), 1)

    assert got[0].open_time == T0 + timedelta(minutes=1)


def test_backoff_minimum_floor_is_respected() -> None:
    assert ExponentialBackoff(base=1, cap=2).next_delay(minimum=30) == 30
