"""Ultimo ciclo do runner: TTL do emergency-stop --liquidate, tentativas persistidas, pausas como conjunto."""

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from engine.runner.paper_control import Command, CommandName, CommandQueue
from engine.runner.paper_state import RunnerSnapshot
from tests.unit.backtest.engine_helpers import T0
from tests.unit.runner.fakes import Harness
from tests.unit.runner.test_paper_reverify2_qa import _persistence_down_robot


def _command(name: CommandName, **kw: Any) -> Command:
    values: dict[str, Any] = {"name": name, "operator": "ana", "reason": "incidente", "issued_at": T0} | kw
    return Command.model_validate(values)


def test_emergency_stop_with_liquidate_expires_but_the_plain_one_never_does(tmp_path: Path) -> None:
    queue = CommandQueue(tmp_path)
    queue.send(_command(CommandName.EMERGENCY_STOP, liquidate=True))
    queue.send(_command(CommandName.EMERGENCY_STOP, issued_at=T0 + timedelta(seconds=1)))

    found = queue.pending(T0 + timedelta(minutes=31))

    assert [(c.name, c.liquidate) for _, c in found] == [(CommandName.EMERGENCY_STOP, False)]
    assert len(list((tmp_path / "expired").glob("*.json"))) == 1


def test_emergency_stop_with_liquidate_is_valid_inside_its_ttl(tmp_path: Path) -> None:
    queue = CommandQueue(tmp_path)
    queue.send(_command(CommandName.EMERGENCY_STOP, liquidate=True))

    assert len(queue.pending(T0 + timedelta(minutes=29))) == 1


async def test_deterministic_command_failure_is_counted_on_disk_and_moved_to_failed_on_the_third(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = await Harness().start()
    h.robot.commands = (queue := CommandQueue(tmp_path))
    queue.send(_command(CommandName.PAUSE, issued_at=h.clock.now()))

    async def broken(*_: Any, **__: Any) -> None:
        raise ValueError("comando com defeito")

    monkeypatch.setattr(h.robot, "_execute", broken)

    with pytest.raises(ValueError, match="defeito"):
        await h.robot._consume_commands(h.clock.now())
    revived = await Harness().start()  # outro processo: a contagem vem do disco, nao da memoria
    revived.robot.commands = CommandQueue(tmp_path)
    monkeypatch.setattr(revived.robot, "_execute", broken)
    with pytest.raises(ValueError, match="defeito"):
        await revived.robot._consume_commands(revived.clock.now())
    await revived.robot._consume_commands(revived.clock.now())

    assert len(list((tmp_path / "failed").glob("*.json"))) == 1
    assert queue.pending() == [] and _attempt_files(tmp_path) == []
    assert "command_rejected" in [a["alert"] for a in revived.bus.of("alerts")]


async def test_status_lists_every_pause_reason_and_a_new_reason_during_the_outage_is_recorded(
    tmp_path: Path,
) -> None:
    h = await _persistence_down_robot(tmp_path)

    assert await h.robot._pause("posicao sem stop ativo na corretora")
    assert not await h.robot._pause("posicao sem stop ativo na corretora")

    reasons = [r["reason"] for r in h.robot.status_payload(h.clock.now())["pause_reasons"]]
    assert len(reasons) == 2 and reasons[1] == "posicao sem stop ativo na corretora"


async def test_reconciliation_is_reported_as_suspended_during_the_outage_and_runs_before_the_resume(
    tmp_path: Path,
) -> None:
    h = await _persistence_down_robot(tmp_path)
    h.clock.advance(seconds=h.robot.config.reconcile_seconds + 1)
    await h.robot.housekeeping()
    assert h.robot._reconcile_suspended

    h.journal.outage = set()
    await h.robot.housekeeping()

    alerts = [a["alert"] for a in h.bus.of("alerts")]
    assert alerts.index("reconcile_suspended") < alerts.index("persistence_recovered")
    assert not h.robot._reconcile_suspended and not h.robot.state.paused


async def test_operator_pause_always_prevails_over_the_automatic_resume(tmp_path: Path) -> None:
    h = await _persistence_down_robot(tmp_path)
    await h.robot._pause("operador segurou o robo", operator="ana")

    h.journal.outage = set()
    await h.robot.housekeeping()

    assert h.robot.state.paused and h.robot.state.pause_reason == "operador segurou o robo"
    assert [r["operator"] for r in h.robot.status_payload(h.clock.now())["pause_reasons"]] == ["ana"]


def test_runner_snapshot_of_an_old_blob_has_no_cursor_and_no_pauses() -> None:
    snapshot = RunnerSnapshot.model_validate({"lives": [], "handled": {}})

    assert snapshot.seen_seq is None and snapshot.pauses == ()


def _attempt_files(directory: Path) -> list[Path]:
    return list(directory.glob("*.attempts"))
