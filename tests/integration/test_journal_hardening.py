"""Endurecimento do diario: snapshots de equity em conflito, decisoes por run e replay de saida com ordem."""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from engine.application.backtest.models import EquityPoint
from engine.domain.order import OrderStatus
from engine.infrastructure.persistence.order_repository import OrderConflictError
from engine.infrastructure.persistence.run_repository import EquityConflictError
from engine.infrastructure.persistence.unit_of_work import UnitOfWork
from tests.integration.journal_helpers import T0, decision, new_run, order
from tests.integration.test_journal_integrity import entry_kwargs, exit_kwargs

pytestmark = pytest.mark.integration

D = Decimal
Sessions = async_sessionmaker[AsyncSession]


@pytest.fixture
async def run_id(sessions: Sessions) -> str:
    run = new_run()
    async with UnitOfWork(sessions) as uow:
        await uow.runs.start(run)
    return run.run_id


def point(equity: str = "1000", *, realized: str = "0", in_position: bool = False) -> EquityPoint:
    return EquityPoint(at=T0, equity=D(equity), realized=D(realized), in_position=in_position)


async def test_equity_snapshot_replay_is_idempotent_but_different_values_conflict(
    sessions: Sessions, run_id: str
) -> None:
    async with UnitOfWork(sessions) as uow:
        assert await uow.equity.record(run_id, point()) is True
    async with UnitOfWork(sessions) as uow:
        assert await uow.equity.record(run_id, point()) is False
    for changed in (point("999"), point(realized="1"), point(in_position=True)):
        with pytest.raises(EquityConflictError):
            async with UnitOfWork(sessions) as uow:
                await uow.equity.record(run_id, changed)
    async with UnitOfWork(sessions) as uow:
        assert [p.equity for p in await uow.equity.list_for_run(run_id)] == [D(1000)]


async def test_signal_decisions_are_scoped_to_their_run(sessions: Sessions, run_id: str) -> None:
    other = new_run()
    async with UnitOfWork(sessions) as uow:
        await uow.runs.start(other)
        await uow.decisions.record(run_id, decision("mine", signal_id="sig-shared"))
        await uow.decisions.record(other.run_id, decision("theirs", signal_id="sig-shared"))
    async with UnitOfWork(sessions) as uow:
        assert [d.code for d in await uow.decisions.list_for_signal(run_id, "sig-shared")] == ["mine"]
        assert [d.code for d in await uow.decisions.list_for_signal(other.run_id, "sig-shared")] == ["theirs"]


async def test_replayed_exit_with_a_different_order_is_not_silently_accepted(
    sessions: Sessions, run_id: str
) -> None:
    async with UnitOfWork(sessions) as uow:
        await uow.record_entry(run_id, **entry_kwargs())
    async with UnitOfWork(sessions) as uow:
        await uow.record_exit(run_id, **exit_kwargs())

    tampered = exit_kwargs()
    tampered["order"] = order("sell-1", side="sell", status=OrderStatus.FILLED).model_copy(
        update={"quantity": D(3), "updated_at": T0 + timedelta(hours=4)}
    )
    with pytest.raises(OrderConflictError):
        async with UnitOfWork(sessions) as uow:
            await uow.record_exit(run_id, **tampered)
