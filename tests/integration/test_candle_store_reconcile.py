from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from engine.domain.candle import Candle
from engine.domain.types import Symbol, Timeframe
from engine.infrastructure.persistence.candle_repository import CandleRepository
from engine.runner.candle_store import backfill_store

pytestmark = pytest.mark.integration

SYMBOL = "RECON/USDT"
T0 = datetime(2020, 1, 1, tzinfo=UTC)
H1 = Timeframe.H1


def candle(i: int, *, close: str = "10.5") -> Candle:
    return Candle(
        symbol=SYMBOL,
        timeframe=H1,
        open_time=T0 + timedelta(hours=i),
        open=Decimal("10.2"),
        high=Decimal(11),
        low=Decimal(10),
        close=Decimal(close),
        volume=Decimal("1.5"),
        closed=True,
    )


class RestFake:
    def __init__(self, candles: list[Candle]) -> None:
        self.candles = candles
        self.calls = 0

    async def fetch_candles(
        self, symbol: Symbol, timeframe: Timeframe, start: datetime, end: datetime | None = None
    ) -> list[Candle]:
        self.calls += 1
        return [c for c in self.candles if c.open_time >= start and (end is None or c.open_time < end)]


@pytest.fixture(autouse=True)
async def _clean(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM candles WHERE symbol = :s"), {"s": SYMBOL})


async def test_backfill_fills_gaps_and_rest_overwrites_divergent_row(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    repo = CandleRepository(sessions)
    truth = [candle(i) for i in range(20)]
    await repo.upsert_many([candle(i) for i in (*range(0, 8), *range(12, 20))])
    await repo.upsert_many([candle(5, close="10.9")])
    rest = RestFake(truth)
    now = T0 + timedelta(hours=20, minutes=3)

    result = await backfill_store(repo, rest, SYMBOL, H1, now, limit=20, reconcile=True)

    assert (result.reconciled, result.gaps, result.persisted, result.unrecovered) == (1, 1, 4, 0)
    assert await repo.fetch_range(SYMBOL, H1, T0, T0 + timedelta(hours=20)) == truth
    again = await backfill_store(repo, rest, SYMBOL, H1, now, limit=20, reconcile=True)
    assert (again.reconciled, again.gaps) == (0, 0)
