from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from engine.domain.candle import Candle
from engine.domain.types import Symbol, Timeframe
from engine.infrastructure.persistence.models import CandleRow

# asyncpg limita 32767 parametros por statement; 8 colunas * 2000 linhas cabe com folga
_CHUNK = 2000


class CandleRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def upsert_many(self, candles: Sequence[Candle]) -> int:
        if any(not c.closed for c in candles):
            raise ValueError("only closed candles can be persisted")
        # ON CONFLICT DO UPDATE nao aceita a mesma chave duas vezes no mesmo comando: vence a ultima
        unique = list({(c.symbol, c.timeframe, c.open_time): c for c in candles}.values())
        async with self._sessions.begin() as session:
            for start in range(0, len(unique), _CHUNK):
                rows = [_to_row(c) for c in unique[start : start + _CHUNK]]
                stmt = insert(CandleRow).values(rows)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["symbol", "timeframe", "open_time"],
                    set_={col: stmt.excluded[col] for col in ("open", "high", "low", "close", "volume")},
                )
                await session.execute(stmt)
        return len(unique)

    async def fetch_range(
        self, symbol: Symbol, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[Candle]:
        stmt = (
            select(CandleRow)
            .where(
                CandleRow.symbol == symbol,
                CandleRow.timeframe == timeframe.value,
                CandleRow.open_time >= start,
                CandleRow.open_time < end,
            )
            .order_by(CandleRow.open_time)
        )
        async with self._sessions() as session:
            result = await session.scalars(stmt)
            return [_to_candle(row) for row in result]

    async def last_open_time(self, symbol: Symbol, timeframe: Timeframe) -> datetime | None:
        stmt = (
            select(CandleRow.open_time)
            .where(CandleRow.symbol == symbol, CandleRow.timeframe == timeframe.value)
            .order_by(CandleRow.open_time.desc())
            .limit(1)
        )
        async with self._sessions() as session:
            return await session.scalar(stmt)


def _to_row(candle: Candle) -> dict[str, object]:
    return {
        "symbol": candle.symbol,
        "timeframe": candle.timeframe.value,
        "open_time": candle.open_time,
        "open": candle.open,
        "high": candle.high,
        "low": candle.low,
        "close": candle.close,
        "volume": candle.volume,
    }


def _to_candle(row: CandleRow) -> Candle:
    return Candle(
        symbol=row.symbol,
        timeframe=Timeframe(row.timeframe),
        open_time=row.open_time,
        open=row.open,
        high=row.high,
        low=row.low,
        close=row.close,
        volume=row.volume,
        closed=True,
    )
