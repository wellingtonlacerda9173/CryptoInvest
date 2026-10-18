from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from engine.domain.candle import Candle
from engine.domain.types import Timeframe
from engine.infrastructure.persistence.audit_repository import AuditRepository
from engine.infrastructure.persistence.candle_repository import CandleRepository

pytestmark = pytest.mark.integration

SYMBOL = "TEST/USDT"
T0 = datetime(2020, 1, 1, tzinfo=UTC)


def candle(i: int, *, close: str = "10.5", closed: bool = True) -> Candle:
    return Candle(
        symbol=SYMBOL,
        timeframe=Timeframe.M1,
        open_time=T0 + timedelta(minutes=i),
        open=Decimal("10.123456789012345678"),
        high=Decimal(11),
        low=Decimal(10),
        close=Decimal(close),
        volume=Decimal("1.5"),
        closed=closed,
    )


@pytest.fixture(autouse=True)
async def _clean(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM candles WHERE symbol = :s"), {"s": SYMBOL})


async def test_candles_is_hypertable(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT count(*) FROM timescaledb_information.hypertables WHERE hypertable_name = 'candles'")
        )
    assert result.scalar_one() == 1


async def test_roundtrip_preserves_decimal_precision_and_utc(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    repo = CandleRepository(sessions)
    await repo.upsert_many([candle(i) for i in range(5)])
    loaded = await repo.fetch_range(SYMBOL, Timeframe.M1, T0, T0 + timedelta(minutes=10))
    assert loaded == [candle(i) for i in range(5)]
    assert loaded[0].open == Decimal("10.123456789012345678")
    assert await repo.last_open_time(SYMBOL, Timeframe.M1) == T0 + timedelta(minutes=4)


async def test_upsert_is_idempotent_and_updates(sessions: async_sessionmaker[AsyncSession]) -> None:
    repo = CandleRepository(sessions)
    await repo.upsert_many([candle(0), candle(1)])
    await repo.upsert_many([candle(1, close="10.9")])
    loaded = await repo.fetch_range(SYMBOL, Timeframe.M1, T0, T0 + timedelta(minutes=10))
    assert [c.close for c in loaded] == [Decimal("10.5"), Decimal("10.9")]


async def test_open_candles_are_rejected(sessions: async_sessionmaker[AsyncSession]) -> None:
    with pytest.raises(ValueError, match="closed"):
        await CandleRepository(sessions).upsert_many([candle(0, closed=False)])


async def test_large_batch_is_chunked(sessions: async_sessionmaker[AsyncSession]) -> None:
    repo = CandleRepository(sessions)
    await repo.upsert_many([candle(i) for i in range(4500)])
    loaded = await repo.fetch_range(SYMBOL, Timeframe.M1, T0, T0 + timedelta(days=4))
    assert len(loaded) == 4500


async def test_audit_log_append(sessions: async_sessionmaker[AsyncSession]) -> None:
    row_id = await AuditRepository(sessions).append("test", "created", "candle", {"n": 1})
    assert row_id > 0


async def test_upsert_many_with_duplicate_keys_in_same_batch_is_idempotent(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    repo = CandleRepository(sessions)

    await repo.upsert_many([candle(0, close="10.4"), candle(0, close="10.6")])

    stored = await repo.fetch_range(SYMBOL, Timeframe.M1, T0, T0 + timedelta(minutes=1))
    assert [c.close for c in stored] == [Decimal("10.6")]


async def test_audit_append_accepts_decimal_and_datetime_payload(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    audit = AuditRepository(sessions)

    row_id = await audit.append("test", "x", "e", {"price": Decimal("1.5"), "at": T0})

    assert row_id > 0
