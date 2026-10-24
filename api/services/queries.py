"""Consultas de leitura do diário (SQLAlchemy sem rastreamento de mudanças; nenhuma escrita)."""

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from engine.infrastructure.persistence.models import (
    ControlCommandRow,
    EquitySnapshotRow,
    PositionRow,
    RunRow,
)

READ_MODES = ("paper",)


async def latest_run(db: AsyncSession, run_id: str | None = None, mode: str = "paper") -> RunRow | None:
    if run_id is not None:
        return await db.get(RunRow, run_id)
    stmt = select(RunRow).where(RunRow.mode == mode).order_by(RunRow.started_at.desc()).limit(1)
    return await db.scalar(stmt)


async def open_position_rows(db: AsyncSession, run_id: str) -> list[PositionRow]:
    stmt = (
        select(PositionRow)
        .where(PositionRow.run_id == run_id, PositionRow.closed_at.is_(None))
        .order_by(PositionRow.opened_at)
    )
    return list((await db.scalars(stmt)).all())


async def pending_commands(db: AsyncSession) -> int:
    stmt = (
        select(func.count())
        .select_from(ControlCommandRow)
        .where(ControlCommandRow.state == "pending_delivery")
    )
    return int(await db.scalar(stmt) or 0)


async def equity_at(db: AsyncSession, run_id: str, moment: datetime) -> Decimal | None:
    """Patrimônio da última foto até `moment` (inclusive)."""
    stmt = (
        select(EquitySnapshotRow.equity)
        .where(EquitySnapshotRow.run_id == run_id, EquitySnapshotRow.at <= moment)
        .order_by(EquitySnapshotRow.at.desc())
        .limit(1)
    )
    return await db.scalar(stmt)


async def first_equity(db: AsyncSession, run_id: str) -> Decimal | None:
    stmt = (
        select(EquitySnapshotRow.equity)
        .where(EquitySnapshotRow.run_id == run_id)
        .order_by(EquitySnapshotRow.at)
        .limit(1)
    )
    return await db.scalar(stmt)


async def last_equity(db: AsyncSession, run_id: str) -> EquitySnapshotRow | None:
    stmt = (
        select(EquitySnapshotRow)
        .where(EquitySnapshotRow.run_id == run_id)
        .order_by(EquitySnapshotRow.at.desc())
        .limit(1)
    )
    return await db.scalar(stmt)


def as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
