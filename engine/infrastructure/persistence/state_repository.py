from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from engine.application.risk import RiskState
from engine.infrastructure.persistence.models import GatewayStateRow, RiskStateRow
from engine.infrastructure.persistence.serialization import dump_model, require_utc, to_json


@dataclass(frozen=True, slots=True)
class GatewaySnapshot:
    version: int
    saved_at: datetime
    state: dict[str, Any]


async def _next_version(
    session: AsyncSession, row: type[RiskStateRow] | type[GatewayStateRow], run_id: str
) -> int:
    # corrida entre dois escritores cai na UNIQUE (run_id, version): um deles falha em vez de sobrescrever
    current = await session.scalar(select(func.max(row.version)).where(row.run_id == run_id))
    return (current or 0) + 1


class RiskStateRepository:
    """Snapshots versionados do `RiskState` (JSONB, Decimal como string: sem perda de precisao)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save(self, run_id: str, state: RiskState, *, saved_at: datetime | None = None) -> int:
        version = await _next_version(self._session, RiskStateRow, run_id)
        self._session.add(
            RiskStateRow(
                run_id=run_id,
                version=version,
                saved_at=require_utc(saved_at or datetime.now(UTC), "saved_at"),
                state=dump_model(state),
            )
        )
        await self._session.flush()
        return version

    async def load_latest(self, run_id: str) -> RiskState | None:
        stmt = (
            select(RiskStateRow.state)
            .where(RiskStateRow.run_id == run_id)
            .order_by(RiskStateRow.version.desc())
            .limit(1)
        )
        data = await self._session.scalar(stmt)
        return RiskState.model_validate(data) if data is not None else None


class GatewayStateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save(self, run_id: str, state: Mapping[str, Any], *, saved_at: datetime | None = None) -> int:
        version = await _next_version(self._session, GatewayStateRow, run_id)
        self._session.add(
            GatewayStateRow(
                run_id=run_id,
                version=version,
                saved_at=require_utc(saved_at or datetime.now(UTC), "saved_at"),
                state=to_json(dict(state)),
            )
        )
        await self._session.flush()
        return version

    async def load_latest(self, run_id: str) -> GatewaySnapshot | None:
        stmt = (
            select(GatewayStateRow)
            .where(GatewayStateRow.run_id == run_id)
            .order_by(GatewayStateRow.version.desc())
            .limit(1)
        )
        row = await self._session.scalar(stmt)
        return GatewaySnapshot(row.version, row.saved_at, row.state) if row else None
