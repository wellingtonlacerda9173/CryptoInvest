from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from engine.application.ai.ledger import AiCallRecord, AiUsage
from engine.infrastructure.persistence.models import AiCallRow
from engine.infrastructure.persistence.serialization import require_utc

_RESERVE_LOCK_KEY = 6_061_001


class AiCallRepository:
    """Append-only: so `add` e leituras (o banco recusa UPDATE/DELETE/TRUNCATE)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, call: AiCallRecord) -> None:
        self._session.add(
            AiCallRow(**{**call.model_dump(), "called_at": require_utc(call.called_at, "called_at")})
        )
        await self._session.flush()

    async def usage(self, month_start: datetime) -> AiUsage:
        """Gasto = custo das chamadas liquidadas + custo de pior caso das reservas ainda sem resultado."""
        start = require_utc(month_start, "month_start")
        stmt = (
            select(AiCallRow.result, func.count(), func.coalesce(func.sum(AiCallRow.cost_usd), 0))
            .where(AiCallRow.called_at >= start, AiCallRow.result != "reserve")
            .group_by(AiCallRow.result)
        )
        rows = (await self._session.execute(stmt)).all()
        settle = aliased(AiCallRow)
        open_reserved = (
            await self._session.execute(
                select(func.coalesce(func.sum(AiCallRow.cost_usd), 0)).where(
                    AiCallRow.called_at >= start,
                    AiCallRow.result == "reserve",
                    ~select(settle.id).where(settle.reservation_id == AiCallRow.id).exists(),
                )
            )
        ).scalar_one()
        return AiUsage(
            month_start=start,
            spent_usd=sum((Decimal(r[2]) for r in rows), Decimal(0)) + Decimal(open_reserved),
            calls=sum(int(r[1]) for r in rows),
            by_result={str(r[0]): int(r[1]) for r in rows},
        )

    async def reserve(self, call: AiCallRecord, budget: Decimal, month_start: datetime) -> int | None:
        await self._session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _RESERVE_LOCK_KEY})
        if (await self.usage(month_start)).spent_usd + call.cost_usd > budget:
            return None
        row = AiCallRow(**{**call.model_dump(), "called_at": require_utc(call.called_at, "called_at")})
        self._session.add(row)
        await self._session.flush()
        return row.id


class SqlAiCallStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def record(self, call: AiCallRecord) -> None:
        async with self._sessions() as session, session.begin():
            await AiCallRepository(session).add(call)

    async def usage(self, month_start: datetime) -> AiUsage:
        async with self._sessions() as session:
            return await AiCallRepository(session).usage(month_start)

    async def reserve(self, call: AiCallRecord, *, budget: Decimal, month_start: datetime) -> int | None:
        async with self._sessions() as session, session.begin():
            return await AiCallRepository(session).reserve(call, budget, month_start)

    async def settle(self, call: AiCallRecord, reservation_id: int) -> None:
        await self.record(call.model_copy(update={"reservation_id": reservation_id}))
