import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration

INSERT = text(
    "INSERT INTO candles (symbol, timeframe, open_time, open, high, low, close, volume) "
    "VALUES ('CHK/USDT', '1m', now(), :o, :h, :l, :c, :v)"
)


@pytest.fixture(autouse=True)
async def _clean(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM candles WHERE symbol = 'CHK/USDT'"))


@pytest.mark.parametrize(
    "values",
    [
        {"o": 0, "h": 1, "l": 0, "c": 0, "v": 1},
        {"o": 1, "h": 1, "l": 0, "c": 1, "v": 1},
        {"o": 1, "h": 0.5, "l": 0.4, "c": 0.5, "v": 1},
        {"o": 1, "h": 2, "l": 1, "c": 1, "v": -1},
    ],
    ids=["zero_open", "zero_low", "high_below_open", "negative_volume"],
)
async def test_database_rejects_invalid_candle_rows(engine: AsyncEngine, values: dict[str, float]) -> None:
    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await conn.execute(INSERT, values)


async def test_database_accepts_valid_candle_row(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(INSERT, {"o": 1, "h": 2, "l": 1, "c": 2, "v": 0})
