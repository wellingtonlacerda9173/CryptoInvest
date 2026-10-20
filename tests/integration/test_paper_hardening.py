"""Endurecimento do runner no Timescale descartavel: locks, outbox, HALT atomico/herdado e relatorio."""

from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import URL
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from engine.domain.decision import DecisionAction
from engine.domain.journal import ControlAction, ControlEvent, DecisionEntry, RunMode, RunRecord
from engine.domain.order import OrderStatus
from engine.infrastructure.config.settings import Settings
from engine.infrastructure.persistence.session import create_engine, create_session_factory
from engine.infrastructure.persistence.unit_of_work import UnitOfWork
from engine.runner.paper_config import RunPaths
from engine.runner.paper_journal import SqlJournal
from engine.runner.paper_lock import AlreadyRunningError, database_locks
from engine.runner.paper_report_data import used_open_calendar
from engine.runner.paper_robot import BotState
from tests.integration.disposable_db import assert_disposable_database, disposable_database, upgrade_database
from tests.unit.backtest.engine_helpers import T0
from tests.unit.runner.fakes import FakeFeed, Harness, candle_at

pytestmark = pytest.mark.integration

D = Decimal
SIGNAL = ("100", "100.5", "99.5", "100")
FILL = ("100", "101", "99.5", "100.5")
STOP = ("100.5", "100.6", "94", "95")
ENTRY = "e2601020600BTC"


def run_name() -> str:
    return f"hd-{uuid4().hex[:12]}"


class DownJournal(SqlJournal):
    """Diario SQL cujo banco 'cai' (erro de conexao do driver) enquanto `down` for verdadeiro."""

    down = False


def _gate(name: str) -> Any:
    original = getattr(SqlJournal, name)

    async def wrapper(self: DownJournal, *args: Any, **kwargs: Any) -> Any:
        if self.down:
            raise OperationalError(name, {}, ConnectionResetError("conexao perdida"))
        return await original(self, *args, **kwargs)

    return wrapper


for _name in (
    "record_decision",
    "update_order",
    "record_entry",
    "record_exit",
    "record_position",
    "record_control",
    "save_state",
    "record_equity",
):
    setattr(DownJournal, _name, _gate(_name))


# ---------------------------------------------------------------- M5: locks no banco


async def test_advisory_locks_allow_one_robot_per_run_and_per_symbol(scratch_engine: AsyncEngine) -> None:
    run, other = run_name(), run_name()
    async with database_locks(scratch_engine, run, ["BTC/USDT"]):
        with pytest.raises(AlreadyRunningError, match="run:"):
            async with database_locks(scratch_engine, run, ["ETH/USDT"]):
                pytest.fail("segundo robo no mesmo run nao pode entrar")
        with pytest.raises(AlreadyRunningError, match="symbol:BTC/USDT"):
            async with database_locks(scratch_engine, other, ["BTC/USDT"]):
                pytest.fail("segundo robo no mesmo simbolo nao pode entrar")
        async with database_locks(scratch_engine, other, ["ETH/USDT"]):
            pass

    # liberados ao sair (e nada vazou da tentativa recusada): o mesmo run e o simbolo voltam a ficar livres
    async with database_locks(scratch_engine, run, ["BTC/USDT"]):
        pass


# ---------------------------------------------------------------- A3: banco fora e outbox


async def test_outbox_reapplies_entry_and_exit_in_the_real_journal_after_an_outage(
    scratch_sessions: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    journal = DownJournal(scratch_sessions, run_name())
    h = await Harness(journal=journal, paths=RunPaths(tmp_path, "r")).start()
    await h.close(candle_at(29, *SIGNAL))
    journal.down = True

    await h.close(candle_at(30, *FILL))
    await h.close(candle_at(31, *STOP))

    assert h.robot.bot_state is BotState.FAILED_PERSISTENCE
    assert h.robot.state.paused and len(h.robot.state.ledger) == 1
    async with UnitOfWork(scratch_sessions) as uow:
        assert await uow.trades.list_for_run(journal.run_id) == []
        assert await uow.fills.list_for_run(journal.run_id) == []
    journal.down = False
    await h.robot.housekeeping()

    assert h.robot.bot_state is BotState.RUNNING and len(h.robot.journal.outbox) == 0
    async with UnitOfWork(scratch_sessions) as uow:
        [trade] = await uow.trades.list_for_run(journal.run_id)
        assert trade.trade_id == h.robot.state.ledger[0].trade_id
        assert {f.client_order_id for f in await uow.fills.list_for_run(journal.run_id)} == {
            ENTRY,
            f"{ENTRY}:oco",
        }
        assert await uow.positions.list_open(journal.run_id) == []
        oco = await uow.orders.get(journal.run_id, f"{ENTRY}:oco")
        assert oco is not None and oco.status is OrderStatus.FILLED
        saved = await uow.risk_states.load_latest(journal.run_id)
        assert saved is not None and saved.equity == h.robot.state.equity and not saved.paused


# ---------------------------------------------------------------- M5: HALT herdado (banco isolado)


@pytest.fixture
def isolated_url(settings: Settings, db_ready: None) -> Iterator[URL]:
    """Banco proprio do teste: um run HALTED nao pode ser herdado por outros runs do banco compartilhado."""
    with disposable_database(settings, "inherit") as url:
        upgrade_database(url)
        yield url


@pytest_asyncio.fixture
async def isolated_sessions(isolated_url: URL) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    assert_disposable_database(isolated_url)
    engine = create_engine(isolated_url)
    yield create_session_factory(engine)
    await engine.dispose()


# ---------------------------------------------------------------- M1 / M3: saida atomica


async def test_halt_event_is_written_with_the_trade_or_not_at_all(
    isolated_sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    scratch_sessions = (
        isolated_sessions  # o run termina HALTED: banco proprio, para nao ser herdado por outros
    )
    journal = SqlJournal(scratch_sessions, run_name())
    h = await Harness(journal=journal).start()
    await h.close(candle_at(29, *SIGNAL))
    await h.close(candle_at(30, *FILL))
    h.robot.state = h.robot.state.model_copy(update={"peak_equity": D(10_560)})
    await h.robot._save_state(h.clock.now())

    async def broken(*_: object, **__: object) -> None:
        raise RuntimeError("falha injetada ao gravar o evento de controle")

    monkeypatch.setattr(
        "engine.infrastructure.persistence.run_repository.ControlEventRepository.append", broken
    )
    with pytest.raises(RuntimeError, match="falha injetada"):
        await h.close(candle_at(31, *STOP))
    monkeypatch.undo()

    async with UnitOfWork(scratch_sessions) as uow:
        assert await uow.trades.list_for_run(journal.run_id) == []
        assert [e.action for e in await uow.control_events.list_for_run(journal.run_id)] == [
            ControlAction.START
        ]
        saved = await uow.risk_states.load_latest(journal.run_id)
        assert saved is not None and not saved.halted and saved.ledger == ()

    down = [candle_at(31, *STOP), candle_at(32, "95", "96", "94.5", "95.5")]
    h.clock.set(down[-1].close_time + timedelta(seconds=30))
    candles = [*h.history, candle_at(29, *SIGNAL), candle_at(30, *FILL), *down]
    revived = await Harness(journal=journal, history=h.history, feed=FakeFeed(candles), clock=h.clock).start()

    assert revived.robot.state.halted
    async with UnitOfWork(scratch_sessions) as uow:
        [trade] = await uow.trades.list_for_run(journal.run_id)
        actions = [e.action for e in await uow.control_events.list_for_run(journal.run_id)]
        assert ControlAction.HALT in actions
        saved = await uow.risk_states.load_latest(journal.run_id)
        assert saved is not None and saved.halted and [t.trade_id for t in saved.ledger] == [trade.trade_id]


async def test_market_exit_closes_the_oco_row_in_the_real_journal(
    scratch_sessions: async_sessionmaker[AsyncSession],
) -> None:
    journal = SqlJournal(scratch_sessions, run_name())
    h = await Harness(journal=journal).start()
    await h.close(candle_at(29, *SIGNAL))
    await h.close(candle_at(30, *FILL))
    await h.close(candle_at(31, "100.5", "108", "100.4", "104"))
    h.clock.advance(seconds=1)
    await h.tick("103.9", "104.1")

    assert h.robot.live_symbols == []
    async with UnitOfWork(scratch_sessions) as uow:
        oco = await uow.orders.get(journal.run_id, f"{ENTRY}:oco")
        exit_order = await uow.orders.get(journal.run_id, f"{ENTRY}:x1")
        assert oco is not None and oco.status is OrderStatus.CANCELED
        assert exit_order is not None and exit_order.status is OrderStatus.FILLED
        assert await uow.orders.list_open(journal.run_id) == []


async def test_new_run_inherits_the_halt_of_the_previous_run(
    isolated_sessions: async_sessionmaker[AsyncSession],
) -> None:
    first = await Harness(journal=SqlJournal(isolated_sessions, "halt-a")).start()
    await first.close(candle_at(29, *SIGNAL))
    await first.close(candle_at(30, *FILL))
    first.robot.state = first.robot.state.model_copy(update={"peak_equity": D(10_560)})
    await first.close(candle_at(31, *STOP))
    assert first.robot.state.halted

    first.clock.advance(seconds=3600)
    second = await Harness(journal=SqlJournal(isolated_sessions, "halt-b"), clock=first.clock).start()

    assert second.robot.state.halted and second.robot.bot_state is BotState.HALTED
    async with UnitOfWork(isolated_sessions) as uow:
        events = await uow.control_events.list_for_run("halt-b")
        saved = await uow.risk_states.load_latest("halt-b")
    assert [e.action for e in events] == [ControlAction.START, ControlAction.HALT]
    assert "halt-a" in events[-1].reason
    assert saved is not None and saved.halted


# ---------------------------------------------------------------- A2: relatorio sobre o historico inteiro


async def test_used_open_calendar_also_sees_flagged_decisions_not_only_the_starts(
    scratch_sessions: async_sessionmaker[AsyncSession],
) -> None:
    clean, dirty = SqlJournal(scratch_sessions, run_name()), SqlJournal(scratch_sessions, run_name())
    for journal in (clean, dirty):
        run = RunRecord(
            run_id=journal.run_id, mode=RunMode.PAPER, started_at=T0, config_hash="h", code_version="v"
        )
        start = ControlEvent(at=T0, action=ControlAction.START, operator="system", reason="inicio")
        await journal.start_run(run, {"config": {"config": {"allow_open_calendar": False}}}, start)
    for journal, flagged in ((clean, False), (dirty, True)):
        await journal.record_decision(
            DecisionEntry(
                decided_at=T0 + timedelta(hours=1),
                symbol="BTC/USDT",
                action=DecisionAction.SKIP,
                traded=False,
                code="no_signal",
                reason="sem sinal",
                context={"calendar_open": flagged},
            )
        )

    async with scratch_sessions() as session:
        assert await used_open_calendar(session, dirty.run_id) is True
        assert await used_open_calendar(session, clean.run_id) is False
