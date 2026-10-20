from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from engine.application.backtest.models import EquityPoint
from engine.domain.journal import ControlEvent, RunRecord
from engine.infrastructure.persistence.models import ControlEventRow, EquitySnapshotRow, RunRow
from engine.infrastructure.persistence.serialization import (
    ConflictError,
    PersistenceError,
    affected,
    quantize,
    require_utc,
)


class EquityConflictError(ConflictError):
    """Ja existe um snapshot neste instante com valores diferentes; a curva nao e reescrita."""


def _run(row: RunRow) -> RunRecord:
    return RunRecord.model_validate(row, from_attributes=True)


class RunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def start(self, run: RunRecord) -> None:
        """Idempotente: religar o mesmo run_id com os mesmos modo/config e aceito; divergente e erro."""
        stmt = (
            insert(RunRow).values(**run.model_dump()).on_conflict_do_nothing(index_elements=[RunRow.run_id])
        )
        if affected(await self._session.execute(stmt)) == 1:
            return
        stored = await self.get(run.run_id)
        if stored is None or (stored.mode, stored.config_hash) != (run.mode, run.config_hash):
            raise PersistenceError(f"run {run.run_id} ja existe com modo/config diferentes")

    async def finish(self, run_id: str, ended_at: datetime) -> None:
        stmt = (
            update(RunRow).where(RunRow.run_id == run_id).values(ended_at=require_utc(ended_at, "ended_at"))
        )
        await self._session.execute(stmt)

    async def get(self, run_id: str) -> RunRecord | None:
        row = await self._session.get(RunRow, run_id)
        return _run(row) if row else None


class ControlEventRepository:
    """Append-only: so `append` e leituras."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, run_id: str, event: ControlEvent) -> None:
        self._session.add(
            ControlEventRow(
                run_id=run_id,
                at=event.at,
                action=event.action.value,
                operator=event.operator,
                reason=event.reason,
            )
        )
        await self._session.flush()

    async def list_for_run(self, run_id: str) -> list[ControlEvent]:
        stmt = (
            select(ControlEventRow)
            .where(ControlEventRow.run_id == run_id)
            .order_by(ControlEventRow.at, ControlEventRow.id)
        )
        return [
            ControlEvent.model_validate(row, from_attributes=True)
            for row in await self._session.scalars(stmt)
        ]


class EquitySnapshotRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(self, run_id: str, point: EquityPoint) -> bool:
        """Idempotente por (run, `at`): False se ja existia identico; valores diferentes sao conflito."""
        at = require_utc(point.at, "at")
        stmt = (
            insert(EquitySnapshotRow)
            .values(
                run_id=run_id,
                at=at,
                equity=point.equity,
                realized=point.realized,
                in_position=point.in_position,
            )
            .on_conflict_do_nothing(index_elements=[EquitySnapshotRow.run_id, EquitySnapshotRow.at])
        )
        if affected(await self._session.execute(stmt)) == 1:
            return True
        stored = await self._session.get(EquitySnapshotRow, (run_id, at))
        if stored is None or (
            quantize(stored.equity),
            quantize(stored.realized),
            stored.in_position,
        ) != (quantize(point.equity), quantize(point.realized), point.in_position):
            raise EquityConflictError(
                f"snapshot de equity em {at.isoformat()} ja registrado com valores diferentes"
            )
        return False

    async def list_for_run(
        self, run_id: str, *, since: datetime | None = None, until: datetime | None = None
    ) -> list[EquityPoint]:
        """Pontos em [since, until), em ordem cronologica."""
        stmt = select(EquitySnapshotRow).where(EquitySnapshotRow.run_id == run_id)
        if since is not None:
            stmt = stmt.where(EquitySnapshotRow.at >= require_utc(since, "since"))
        if until is not None:
            stmt = stmt.where(EquitySnapshotRow.at < require_utc(until, "until"))
        rows = await self._session.scalars(stmt.order_by(EquitySnapshotRow.at))
        return [EquityPoint.model_validate(row, from_attributes=True) for row in rows]
