"""Reverificacao 2 (QA): ordem do outbox em disco."""

import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from engine.domain.journal import ControlAction, ControlEvent
from engine.runner.paper_outbox import Outbox, _ControlCall


def _call(i: int) -> _ControlCall:
    return _ControlCall(
        event=ControlEvent(
            at=datetime(2026, 1, 1, tzinfo=UTC), action=ControlAction.START, operator="qa", reason=str(i)
        )
    )


def test_Outbox_PendingDeveManterOrdemDeInsercaoMesmoComRelogioGrosso(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    monkeypatch.setattr(time, "time_ns", lambda: 1_700_000_000_000_000_000)
    outbox = Outbox(tmp_path)

    # Act: 30 anexos em sequencia (o relogio do Windows tem 15,6 ms de resolucao: varios caem no mesmo tick)
    for i in range(30):
        outbox.append(_call(i))
    reasons = [int(call.event.reason) for _, call in outbox.pending()]  # type: ignore[union-attr]

    # Assert
    assert reasons == list(range(30))


async def _persistence_down_robot(tmp_path: Path) -> Any:
    from engine.runner.paper_config import RunPaths
    from tests.unit.runner.fakes import Harness, candle_at
    from tests.unit.runner.test_paper_qa_review import FILL_CANDLE, SIGNAL_CANDLE, OutageJournal

    h = await Harness(journal=OutageJournal(), paths=RunPaths(tmp_path, "test-run")).start()
    await h.close(candle_at(29, *SIGNAL_CANDLE))
    candle = candle_at(30, *FILL_CANDLE)
    h.clock.set(candle.close_time)
    h.journal.outage, h.journal.persistent = {"record_entry"}, True
    await h.robot.on_candle(candle)
    assert h.robot.state.paused and h.robot.journal.down
    return h


async def test_pause_for_another_reason_during_outage_must_survive_the_automatic_resume(
    tmp_path: Path,
) -> None:
    # Arrange
    h = await _persistence_down_robot(tmp_path)
    await h.robot._pause("posicao sem stop ativo na corretora (divergencia real)")

    # Act
    h.journal.outage = set()
    await h.robot.housekeeping()

    # Assert
    assert h.robot.state.paused


async def test_manual_pause_before_the_outage_is_not_lifted_by_the_automatic_resume(tmp_path: Path) -> None:
    # Arrange
    from engine.runner.paper_config import RunPaths
    from tests.unit.runner.fakes import Harness, candle_at
    from tests.unit.runner.test_paper_qa_review import FILL_CANDLE, SIGNAL_CANDLE, OutageJournal

    h = await Harness(journal=OutageJournal(), paths=RunPaths(tmp_path, "test-run")).start()
    await h.close(candle_at(29, *SIGNAL_CANDLE))
    await h.robot._pause("operador pausou para investigar", operator="ana")
    candle = candle_at(30, *FILL_CANDLE)
    h.clock.set(candle.close_time)
    h.journal.outage, h.journal.persistent = {"record_entry"}, True
    await h.robot.on_candle(candle)
    assert h.robot.journal.down

    # Act
    h.journal.outage = set()
    await h.robot.housekeeping()

    # Assert
    assert not h.robot.journal.down
    assert h.robot.state.paused and h.robot.state.pause_reason == "operador pausou para investigar"
