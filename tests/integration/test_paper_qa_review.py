"""Revisao QA do runner de paper no Timescale descartavel: relatorio de runs reiniciados e queda na saida."""

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from engine.application.risk import RiskState
from engine.infrastructure.persistence.trade_repository import TradeRepository
from engine.runner.paper_journal import SqlJournal
from engine.runner.paper_report_data import collect_inputs
from tests.unit.runner.fakes import FakeFeed, Harness, SimulatedCrash, candle_at, config

pytestmark = pytest.mark.integration

SIGNAL = ("100", "100.5", "99.5", "100")
FILL = ("100", "101", "99.5", "100.5")


async def test_BUG_run_restarted_without_the_open_calendar_flag_is_still_flagged_in_the_report(
    scratch_sessions: async_sessionmaker[AsyncSession],
) -> None:
    journal = SqlJournal(scratch_sessions, f"qa-{uuid4().hex[:12]}")
    first = await Harness(journal=journal, cfg=config(allow_open_calendar=True)).start()
    await first.close(candle_at(29, *SIGNAL))

    first.clock.advance(seconds=600)
    await Harness(
        journal=journal,
        history=first.history,
        feed=FakeFeed(first.feed.candles),
        clock=first.clock,
        cfg=config(allow_open_calendar=False),
    ).start()

    inputs = await collect_inputs(
        scratch_sessions, journal.run_id, now=first.clock.now() + timedelta(hours=1), compare=False
    )

    assert inputs.allow_open_calendar is True


class _CrashOnSave(SqlJournal):
    """Diario SQL que 'perde o processo' na proxima gravacao periodica de estado (apos ordens ja gravadas)."""

    armed = False

    async def save_state(self, risk_state: RiskState, gateway_state: Mapping[str, Any], at: datetime) -> None:
        if self.armed:
            self.armed = False
            raise SimulatedCrash("save_state")
        await super().save_state(risk_state, gateway_state, at)


async def test_BUG_crash_after_market_exit_order_is_journaled_must_not_wedge_the_restart(
    scratch_sessions: async_sessionmaker[AsyncSession],
) -> None:
    journal = _CrashOnSave(scratch_sessions, f"qa-{uuid4().hex[:12]}")
    h = await Harness(journal=journal).start()
    await h.close(candle_at(29, *SIGNAL))
    await h.close(candle_at(30, *FILL))
    journal.armed = True
    exit_candle = candle_at(31, "100.5", "108", "100.4", "104")
    with pytest.raises(SimulatedCrash):
        await h.close(exit_candle)

    feed = FakeFeed([*h.history, candle_at(29, *SIGNAL), candle_at(30, *FILL), exit_candle])
    revived = await Harness(journal=journal, history=h.history, feed=feed, clock=h.clock).start()
    for _ in range(3):
        revived.clock.advance(seconds=1)
        await revived.tick("103.9", "104.1")

    assert revived.robot.live_symbols == []
    async with scratch_sessions() as session:
        trades = await TradeRepository(session).list_for_run(journal.run_id)
    assert [t.exit_reason.value for t in trades] == ["profit_protection"]
