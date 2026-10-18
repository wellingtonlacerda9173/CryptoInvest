from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from engine.domain.types import Timeframe
from engine.infrastructure.binance import market_data
from engine.infrastructure.binance.market_data import BinanceMarketData

SYMBOL = "BTC/USDT"
STEP = 60_000
T0 = datetime(2026, 1, 1, tzinfo=UTC)
T0_MS = int(T0.timestamp() * 1000)


class FakeExchange:
    """Simula /klines: respeita startTime/endTime (inclusivo)/limit e registra as chamadas."""

    def __init__(self, total: int, *, error: Exception | None = None, server_now: datetime = T0) -> None:
        self.total = total
        self.error = error
        self.server_now = server_now
        self.calls: list[dict[str, Any]] = []

    async def publicGetTime(self) -> dict[str, int]:
        return {"serverTime": int(self.server_now.timestamp() * 1000)}

    async def publicGetKlines(self, params: dict[str, Any]) -> list[list[Any]]:
        self.calls.append(params)
        if self.error:
            raise self.error
        rows = []
        for i in range(self.total):
            open_ms = T0_MS + i * STEP
            if open_ms >= params["startTime"] and open_ms <= params["endTime"]:
                rows.append([open_ms, "1", "2", "1", "1.5", "10", open_ms + STEP - 1])
        return rows[: params["limit"]]

    async def close(self) -> None:
        return None


class FrozenDatetime(datetime):
    frozen: datetime = T0

    @classmethod
    def now(cls, tz: Any = None) -> "FrozenDatetime":  # type: ignore[override]
        return cls.frozen.astimezone(tz) if tz else cls.frozen  # type: ignore[return-value]


def client_with(
    exchange: FakeExchange,
    monkeypatch: pytest.MonkeyPatch,
    now: datetime,
    *,
    local_now: datetime | None = None,
) -> BinanceMarketData:
    """`now` e o horario do servidor; `local_now` (padrao = now) e o relogio local da maquina."""
    exchange.server_now = now
    FrozenDatetime.frozen = local_now or now
    monkeypatch.setattr(market_data, "datetime", FrozenDatetime)
    client = BinanceMarketData()
    client._exchange = exchange  # type: ignore[assignment]
    return client


async def test_pagination_has_no_gaps_or_duplicates_across_page_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exchange = FakeExchange(total=2500)
    client = client_with(exchange, monkeypatch, T0 + timedelta(minutes=2600))

    candles = await client.fetch_candles(SYMBOL, Timeframe.M1, T0)

    assert [c.open_time for c in candles] == [T0 + timedelta(minutes=i) for i in range(2500)]
    assert len(exchange.calls) == 3


async def test_exact_page_multiple_does_not_loop_forever(monkeypatch: pytest.MonkeyPatch) -> None:
    exchange = FakeExchange(total=2000)
    client = client_with(exchange, monkeypatch, T0 + timedelta(minutes=5000))

    candles = await client.fetch_candles(SYMBOL, Timeframe.M1, T0)

    assert len(candles) == 2000
    assert len(exchange.calls) == 3


async def test_forming_candle_is_excluded(monkeypatch: pytest.MonkeyPatch) -> None:
    exchange = FakeExchange(total=10)
    client = client_with(exchange, monkeypatch, T0 + timedelta(minutes=9, seconds=30))

    candles = await client.fetch_candles(SYMBOL, Timeframe.M1, T0)

    assert candles[-1].open_time == T0 + timedelta(minutes=8)


async def test_start_in_future_returns_empty_without_request(monkeypatch: pytest.MonkeyPatch) -> None:
    exchange = FakeExchange(total=10)
    client = client_with(exchange, monkeypatch, T0)

    assert await client.fetch_candles(SYMBOL, Timeframe.M1, T0 + timedelta(days=1)) == []
    assert exchange.calls == []


@pytest.mark.parametrize("error", [Exception("429 rate limit"), Exception("418 ip banned"), Exception("451")])
async def test_http_errors_are_propagated_not_swallowed(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    client = client_with(FakeExchange(total=10, error=error), monkeypatch, T0 + timedelta(minutes=20))

    with pytest.raises(Exception, match=str(error)):
        await client.fetch_candles(SYMBOL, Timeframe.M1, T0)


async def test_naive_start_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    client = client_with(FakeExchange(total=10), monkeypatch, T0 + timedelta(minutes=20))

    with pytest.raises(ValueError, match="timezone"):
        await client.fetch_candles(SYMBOL, Timeframe.M1, datetime(2026, 1, 1))


async def test_candle_still_forming_on_server_is_not_closed_when_local_clock_is_ahead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exchange = FakeExchange(total=10)
    client = client_with(
        exchange,
        monkeypatch,
        T0 + timedelta(minutes=9, seconds=30),
        local_now=T0 + timedelta(minutes=10, seconds=10),
    )

    candles = await client.fetch_candles(SYMBOL, Timeframe.M1, T0)

    assert candles[-1].open_time == T0 + timedelta(minutes=8)
