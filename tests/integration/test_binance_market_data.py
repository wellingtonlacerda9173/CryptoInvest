import asyncio
import itertools
from datetime import UTC, datetime, timedelta

import pytest

from engine.domain.types import Timeframe
from engine.infrastructure.binance.market_data import BinanceMarketData

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("binance_reachable")]


async def test_fetch_candles_paginates_without_gaps_and_only_closed() -> None:
    end = datetime.now(UTC).replace(second=0, microsecond=0)
    start = end - timedelta(minutes=2500)
    async with BinanceMarketData() as market:
        candles = await market.fetch_candles("BTC/USDT", Timeframe.M1, start, end)

    assert len(candles) >= 2400
    assert all(c.closed for c in candles)
    times = [c.open_time for c in candles]
    assert times == sorted(set(times))
    assert all(b - a == timedelta(minutes=1) for a, b in itertools.pairwise(times))
    assert times[0] == start.replace(second=0, microsecond=0)


async def test_stream_candles_emits_closed_candle() -> None:
    async with BinanceMarketData() as market:
        stream = market.stream_candles("BTC/USDT", Timeframe.M1)
        try:
            candle = await asyncio.wait_for(anext(stream), timeout=90)
        finally:
            await stream.aclose()  # type: ignore[attr-defined]
    assert candle.closed
    assert candle.symbol == "BTC/USDT"


async def test_order_book_stream() -> None:
    async with BinanceMarketData() as market:
        stream = market.stream_order_book("BTC/USDT", depth=5)
        try:
            book = await asyncio.wait_for(anext(stream), timeout=30)
        finally:
            await stream.aclose()  # type: ignore[attr-defined]
    assert book.bids
    assert book.asks
    assert book.bids[0].price < book.asks[0].price
