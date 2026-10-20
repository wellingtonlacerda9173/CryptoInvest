"""Endurecimento do runner de paper: durabilidade, comandos, instancia unica, saida atomica e honestidade."""

import asyncio
import os
import time
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from engine.application.risk import Rejection, RejectionCode
from engine.domain.gateway import DuplicateOrderError
from engine.domain.journal import ControlAction, OrderRecord
from engine.domain.order import OrderStatus, OrderType
from engine.infrastructure.config.logging import scrub_text
from engine.infrastructure.paper import PaperGateway
from engine.infrastructure.persistence.serialization import ConflictError
from engine.runner import paper, paper_robot
from engine.runner.paper_config import RunPaths
from engine.runner.paper_control import Command, CommandName, CommandQueue
from engine.runner.paper_lock import AlreadyRunningError, InstanceLock
from engine.runner.paper_outbox import is_transient
from engine.runner.paper_report import build_cumulative, to_markdown
from engine.runner.paper_robot import BotState, FeedError, _entry_id
from tests.unit.backtest.engine_helpers import T0
from tests.unit.runner.fakes import Harness, MemoryJournal, SimulatedCrash, candle_at
from tests.unit.runner.test_paper_qa_review import (
    OutageJournal,
    _gate_passing_inputs,
)
from tests.unit.runner.test_paper_robot import (
    ENTRY_29,
    FILL_CANDLE,
    SIGNAL_CANDLE,
    STOP_CANDLE,
    entered,
    restart,
)

D = Decimal


def pause_command(now: Any, **kw: Any) -> Command:
    values: dict[str, Any] = {
        "name": CommandName.PAUSE,
        "operator": "ana",
        "reason": "manutencao",
        "issued_at": now,
    } | kw
    return Command.model_validate(values)


# ---------------------------------------------------------------- M6: comandos


def test_stale_commands_expire_and_are_archived_not_deleted(tmp_path: Path) -> None:
    queue = CommandQueue(tmp_path)
    queue.send(pause_command(T0, issued_at=T0))
    queue.send(pause_command(T0, name=CommandName.RESUME, issued_at=T0 + timedelta(minutes=9)))

    live = queue.pending(T0 + timedelta(minutes=11))

    assert [c.name for _, c in live] == [CommandName.RESUME]
    assert [p.name for p in (tmp_path / "expired").glob("*.json")] == [
        f"{T0:%Y%m%dT%H%M%S%f}-pause.json".replace("+0000", "")
    ]


def test_emergency_stop_never_expires_and_ignores_the_start_barrier(tmp_path: Path) -> None:
    queue = CommandQueue(tmp_path)
    queue.send(pause_command(T0, name=CommandName.EMERGENCY_STOP, reason="incidente"))
    queue.send(pause_command(T0, name=CommandName.STOP, issued_at=T0 + timedelta(seconds=1)))

    found = queue.pending(T0 + timedelta(days=3), not_before=T0 + timedelta(days=3))

    assert [c.name for _, c in found] == [CommandName.EMERGENCY_STOP]


async def test_robot_discards_commands_issued_before_its_start_but_honors_emergency_stop(
    tmp_path: Path,
) -> None:
    h = await Harness().start()
    queue = CommandQueue(tmp_path)
    h.robot.commands = queue
    before = h.robot.started_at - timedelta(minutes=1)
    queue.send(pause_command(before, name=CommandName.PAUSE))
    await h.robot.housekeeping()
    assert not h.robot.state.paused and (tmp_path / "expired").is_dir()

    queue.send(pause_command(before, name=CommandName.EMERGENCY_STOP, reason="incidente"))
    await h.robot.housekeeping()

    assert h.robot.state.paused and "emergency-stop" in (h.robot.state.pause_reason or "")


async def test_command_older_than_the_ttl_is_not_executed_by_the_running_robot(tmp_path: Path) -> None:
    h = await Harness().start()
    queue = CommandQueue(tmp_path)
    h.robot.commands = queue
    queue.send(pause_command(h.clock.now()))
    h.clock.advance(seconds=11 * 60)

    await h.robot.housekeeping()

    assert not h.robot.state.paused and queue.pending() == []


async def test_sensitive_commands_are_audited_with_the_command_hash(tmp_path: Path) -> None:
    h = await entered()
    h.robot.commands = (queue := CommandQueue(tmp_path))
    h.robot.state = h.robot.state.model_copy(update={"peak_equity": D(10_560)})
    await h.close(candle_at(31, *STOP_CANDLE))
    assert h.robot.state.halted
    reactivate = pause_command(h.clock.now(), name=CommandName.REACTIVATE, reason="revisado")
    queue.send(reactivate)
    await h.robot.housekeeping()
    liquidate = pause_command(
        h.clock.now() + timedelta(seconds=1),
        name=CommandName.EMERGENCY_STOP,
        liquidate=True,
        reason="incidente",
    )
    queue.send(liquidate)
    await h.robot.housekeeping()

    by_action = {a["action"]: a for a in h.journal.audits if "command_sha256" in a}
    assert by_action["reactivate"]["command_sha256"] == reactivate.digest()
    assert by_action["pause"]["command_sha256"] == liquidate.digest() and by_action["pause"]["liquidate"]
    assert reactivate.sensitive and liquidate.sensitive and not pause_command(T0).sensitive


def test_cli_demands_operator_and_reason_for_a_liquidation(tmp_path: Path) -> None:
    RunPaths(tmp_path, "r1").ensure()
    args = paper.build_parser().parse_args(
        ["emergency-stop", "--run-id", "r1", "--data-dir", str(tmp_path), "--reason", "x", "--liquidate"]
    )

    with pytest.raises(ValueError, match="exige --operator e --reason"):
        paper.run_command(args)
    assert list((tmp_path / "r1" / "commands").glob("*.json")) == []


def test_cli_scrubs_secrets_from_the_operator_reason(tmp_path: Path) -> None:
    RunPaths(tmp_path, "r1").ensure()
    args = paper.build_parser().parse_args(
        ["pause", "--run-id", "r1", "--data-dir", str(tmp_path), "--reason", "ver token=abc123 no painel"]
    )

    paper.run_command(args)

    [(_, sent)] = CommandQueue(RunPaths(tmp_path, "r1").commands).pending()
    assert "abc123" not in sent.reason and "token=***" in sent.reason


@pytest.mark.skipif(os.name != "posix", reason="permissoes POSIX")
def test_command_directory_is_private_where_the_os_allows(tmp_path: Path) -> None:
    paths = RunPaths(tmp_path, "r1")
    paths.ensure()

    assert (paths.commands.stat().st_mode & 0o077) == 0


def test_scrub_text_masks_secrets_in_free_text() -> None:
    assert (
        scrub_text("postgresql+asyncpg://robo:s3nha@db:5432/x caiu")
        == "postgresql+asyncpg://robo:***@db:5432/x caiu"
    )
    assert scrub_text("api_key=XYZ e token: abc") == "api_key=*** e token: ***"
    assert scrub_text("Authorization: Bearer abc.def").count("abc") == 0
    assert scrub_text("conexao recusada em 127.0.0.1:5432") == "conexao recusada em 127.0.0.1:5432"


def test_scrub_text_masks_json_basic_auth_and_passwords_with_at_sign() -> None:
    assert scrub_text('{"api_key": "ABC123", "n": 1}') == '{"api_key": ***, "n": 1}'
    assert "dXNlcjpwYXNz" not in scrub_text("Authorization: Basic dXNlcjpwYXNz")
    masked = scrub_text("postgresql://robo:p@ss@db:5432/x caiu")
    assert masked == "postgresql://robo:***@db:5432/x caiu"


@pytest.mark.parametrize(
    "payload", ["a-" * 200_000, "a" * 200_000, "token-" * 50_000], ids=["dashes", "plain", "token"]
)
def test_scrub_text_has_no_catastrophic_backtracking_on_hostile_input(payload: str) -> None:
    started = time.perf_counter()

    result = scrub_text(payload)

    assert time.perf_counter() - started < 0.2
    assert result.endswith("...[truncado]") or len(result) <= len(payload)


async def test_operator_reason_is_scrubbed_before_it_reaches_the_journal_and_the_bus(tmp_path: Path) -> None:
    h = await Harness().start()
    h.robot.commands = (queue := CommandQueue(tmp_path))
    queue.send(pause_command(h.clock.now(), reason="falha com password=hunter2 no deploy"))

    await h.robot.housekeeping()

    assert "hunter2" not in h.journal.controls[-1].reason
    assert all("hunter2" not in str(p) for _, p in h.bus.events)
    assert "hunter2" not in (h.robot.state.pause_reason or "")


# ---------------------------------------------------------------- M5: instancia unica e HALT herdado


def test_instance_lock_blocks_a_second_robot_and_frees_on_release(tmp_path: Path) -> None:
    first, second = InstanceLock(tmp_path / "robot.lock"), InstanceLock(tmp_path / "robot.lock")
    first.acquire()
    try:
        with pytest.raises(AlreadyRunningError, match="ja existe um robo em execucao"):
            second.acquire()
    finally:
        first.release()

    second.acquire()
    second.release()


async def test_new_run_inherits_the_halt_of_a_previous_run_until_an_explicit_reactivate(
    tmp_path: Path,
) -> None:
    journal = MemoryJournal("run-novo")
    journal.inherited_halt = ("run-antigo", "drawdown 0.0600")
    h = await Harness(journal=journal).start()

    assert h.robot.state.halted and h.robot.bot_state is BotState.HALTED
    assert "run-antigo" in (h.robot.state.halt_reason or "")
    assert journal.controls[-1].action is ControlAction.HALT
    assert [a["alert"] for a in h.bus.of("alerts")] == ["halt_inherited"]
    await h.close(candle_at(29, *SIGNAL_CANDLE))
    assert h.journal.codes() == ["robot_halted"]

    h.robot.commands = (queue := CommandQueue(tmp_path))
    queue.send(pause_command(h.clock.now(), name=CommandName.REACTIVATE, reason="revisado"))
    await h.robot.housekeeping()
    assert not h.robot.state.halted and journal.controls[-1].action is ControlAction.REACTIVATE


async def test_a_resumed_run_does_not_inherit_anything() -> None:
    journal = MemoryJournal()
    h = await Harness(journal=journal).start()
    journal.inherited_halt = ("outro", "x")

    revived = await restart(h)

    assert not revived.robot.state.halted


# ---------------------------------------------------------------- A3: queda do banco


async def test_supervisor_only_swallows_feed_failures_never_persistence_errors() -> None:
    h = await Harness().start()

    async def database_down() -> None:
        raise ConnectionRefusedError("banco")

    async def feed_down() -> None:
        raise FeedError("fluxo caiu")

    crashed = asyncio.create_task(h.robot._supervise("candles:BTC/USDT", database_down))
    retrying = asyncio.create_task(h.robot._supervise("candles:BTC/USDT", feed_down))
    await asyncio.sleep(0.1)
    try:
        assert crashed.done() and isinstance(crashed.exception(), ConnectionRefusedError)
        assert not retrying.done()
    finally:
        retrying.cancel()
        await asyncio.gather(retrying, return_exceptions=True)


async def test_fill_is_not_marked_as_seen_when_its_write_fails() -> None:
    h = await Harness().start()
    await h.close(candle_at(29, *SIGNAL_CANDLE))
    h.journal.crash_on = {"record_entry"}

    with pytest.raises(SimulatedCrash):
        await h.close(candle_at(30, *FILL_CANDLE))

    assert h.robot._seen_seq == 0 and h.journal.fills == {}


def test_only_infrastructure_errors_are_transient() -> None:
    connection = OperationalError("SELECT 1", {}, ConnectionResetError("reset"))
    deterministic = IntegrityError("INSERT", {}, ValueError("duplicate key"))

    assert all(is_transient(e) for e in (OSError(), TimeoutError(), PoolTimeoutError(), connection))
    assert not any(is_transient(e) for e in (deterministic, ConflictError("x"), RuntimeError(), ValueError()))


async def test_housekeeping_survives_a_transient_failure_and_stops_on_a_bug(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = await Harness().start()
    calls: list[int] = []

    async def flaky() -> None:
        calls.append(1)
        if len(calls) == 1:
            raise ConnectionRefusedError("banco")
        h.robot.stop_event.set()

    monkeypatch.setattr(h.robot, "housekeeping", flaky)
    await asyncio.wait_for(h.robot._housekeeping_loop(), 10)
    assert len(calls) == 2

    async def bug() -> None:
        raise ValueError("defeito")

    monkeypatch.setattr(h.robot, "housekeeping", bug)
    h.robot.stop_event.clear()
    with pytest.raises(ValueError, match="defeito"):
        await h.robot._housekeeping_loop()


async def test_shutdown_reapplies_the_outbox_before_saying_goodbye(tmp_path: Path) -> None:
    h = await Harness(journal=OutageJournal(), paths=RunPaths(tmp_path, "test-run")).start()
    await h.close(candle_at(29, *SIGNAL_CANDLE))
    candle = candle_at(30, *FILL_CANDLE)
    h.clock.set(candle.close_time)
    h.journal.outage, h.journal.persistent = {"record_entry"}, True
    await h.robot.on_candle(candle)
    h.journal.outage = set()

    await h.robot.shutdown("teste")

    assert "BTC/USDT" in h.journal.positions and len(h.robot.journal.outbox) == 0
    assert h.journal.controls[-1].action is ControlAction.STOP


async def test_exit_during_an_outage_is_queued_with_its_halt_and_reapplied_together(tmp_path: Path) -> None:
    h = await Harness(journal=OutageJournal(), paths=RunPaths(tmp_path, "test-run")).start()
    await h.close(candle_at(29, *SIGNAL_CANDLE))
    await h.close(candle_at(30, *FILL_CANDLE))
    h.robot.state = h.robot.state.model_copy(update={"peak_equity": D(10_560)})
    h.journal.outage, h.journal.persistent = {"record_exit"}, True

    await h.close(candle_at(31, *STOP_CANDLE))

    assert h.robot.bot_state is BotState.FAILED_PERSISTENCE
    assert h.journal.trades == [] and len(h.robot.state.ledger) == 1
    assert h.robot.status_payload(h.clock.now())["outbox_pending"] >= 1
    h.journal.outage = set()
    await h.robot.housekeeping()

    assert [t.trade_id for t in h.journal.trades] == [t.trade_id for t in h.robot.state.ledger]
    assert ControlAction.HALT in {e.action for e in h.journal.controls}
    assert h.robot.state.halted and h.robot.bot_state is BotState.HALTED


# ---------------------------------------------------------------- A4 / M3: ordens no diario


async def test_crash_after_the_market_exit_is_journaled_does_not_wedge_the_restart() -> None:
    h = await entered()
    exit_candle = candle_at(31, "100.5", "108", "100.4", "104")
    h.journal.crash_on = {"save_state"}
    with pytest.raises(SimulatedCrash):
        await h.close(exit_candle)
    h.journal.crash_on = set()

    revived = await restart(
        h,
        candles=[*h.history, candle_at(29, *SIGNAL_CANDLE), candle_at(30, *FILL_CANDLE), exit_candle],
    )
    for _ in range(3):
        revived.clock.advance(seconds=1)
        await revived.tick("103.9", "104.1")

    assert revived.robot.live_symbols == []
    assert [t.exit_reason.value for t in h.journal.trades] == ["profit_protection"]


async def test_exit_order_id_left_by_an_old_crash_is_never_reused() -> None:
    h = await entered()
    position = h.journal.positions["BTC/USDT"]
    h.journal.orders[f"{ENTRY_29}:x1"] = OrderRecord(
        client_order_id=f"{ENTRY_29}:x1",
        symbol="BTC/USDT",
        side="sell",
        type=OrderType.MARKET,
        quantity=position.quantity,
        created_at=T0,
        updated_at=T0,
    )
    revived = await restart(h)

    assert h.journal.orders[f"{ENTRY_29}:x1"].status is OrderStatus.CANCELED
    await revived.close(candle_at(31, "100.5", "108", "100.4", "104"))
    revived.clock.advance(seconds=1)
    await revived.tick("103.9", "104.1")

    assert f"{ENTRY_29}:x2" in h.journal.orders and revived.robot.live_symbols == []


async def test_exit_id_already_used_by_the_gateway_moves_on_to_the_next(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = await entered()
    original = PaperGateway.place_order

    async def taken(self: PaperGateway, order: Any) -> Any:
        if order.client_order_id.endswith(":x1"):
            raise DuplicateOrderError(order.client_order_id)
        return await original(self, order)

    monkeypatch.setattr(PaperGateway, "place_order", taken)
    await h.close(candle_at(31, "100.5", "108", "100.4", "104"))

    assert f"{ENTRY_29}:x2" in h.journal.orders and f"{ENTRY_29}:x1" not in h.journal.orders


async def test_market_exit_closes_the_oco_row_in_the_same_write_as_the_exit() -> None:
    h = await entered()
    await h.close(candle_at(31, "100.5", "108", "100.4", "104"))
    h.clock.advance(seconds=1)
    await h.tick("103.9", "104.1")

    assert h.journal.orders[f"{ENTRY_29}:oco"].status is OrderStatus.CANCELED
    assert h.journal.orders[f"{ENTRY_29}:x1"].status is OrderStatus.FILLED
    assert [a for a in h.journal.calls if a == "update_order"] != []


# ---------------------------------------------------------------- baixas


def test_each_signal_of_a_candle_gets_its_own_entry_id() -> None:
    candle = candle_at(29, *SIGNAL_CANDLE)

    assert _entry_id(candle) == ENTRY_29
    assert len({_entry_id(candle, i) for i in range(3)}) == 3
    assert all(len(_entry_id(candle, i)) <= 36 for i in range(3))


async def test_duplicate_order_in_the_gateway_is_a_skip_not_a_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    h = await Harness().start()

    async def duplicate(order: Any) -> Any:
        raise DuplicateOrderError(order.client_order_id)

    monkeypatch.setattr(h.robot.gateway, "place_order", duplicate)
    await h.close(candle_at(29, *SIGNAL_CANDLE))

    assert h.journal.codes() == ["duplicate_order"] and h.robot._pending == {}


async def test_dust_fill_is_recorded_alerted_and_sold_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = await Harness().start()
    await h.close(candle_at(29, *SIGNAL_CANDLE))
    dust = Rejection(code=RejectionCode.NOTIONAL_BELOW_MIN, message="fill em poeira")
    monkeypatch.setattr(paper_robot, "open_position", lambda *a, **k: dust)

    await h.close(candle_at(30, *FILL_CANDLE))

    assert "dust_fill" in h.journal.codes() and ENTRY_29 in {
        f.client_order_id for f in h.journal.fills.values()
    }
    assert h.robot.state.paused and "poeira" in (h.robot.state.pause_reason or "")
    assert "dust_fill" in [a["alert"] for a in h.bus.of("alerts")]
    assert f"{ENTRY_29}:emg1" in h.journal.orders
    revived = await restart(h)
    assert revived.robot.live_symbols == ["BTC/USDT"]


async def test_live_without_a_journal_position_is_alerted_and_pauses_the_recovery() -> None:
    h = await entered()
    h.journal.positions.clear()

    revived = await restart(h)

    assert revived.robot.state.paused and "recuperacao inconsistente" in (
        revived.robot.state.pause_reason or ""
    )
    assert "restore_inconsistent" in [a["alert"] for a in revived.bus.of("alerts")]


# ---------------------------------------------------------------- A1: elegibilidade para o Live


def test_open_calendar_run_is_ineligible_in_json_markdown_and_never_shown_as_approved() -> None:
    report = build_cumulative(_gate_passing_inputs(allow_open_calendar=True))
    gate = report["live_gate"]

    assert gate["approved"] is False and gate["eligible_for_live"] is False
    assert gate["verdict"] == "INELEGIVEL: calendario aberto"
    [criterion] = [c for c in gate["criteria"] if c["code"] == "eligible_for_live"]
    assert criterion["met"] is False and criterion["reason"] == "calendario aberto"
    markdown = to_markdown(report)
    assert "INELEGIVEL: calendario aberto" in markdown and "APROVADO" not in markdown


def test_clean_run_is_eligible_and_keeps_the_original_criteria() -> None:
    gate = build_cumulative(_gate_passing_inputs())["live_gate"]

    assert gate["eligible_for_live"] is True and gate["verdict"] == "APROVADO"
    assert {c["code"] for c in gate["criteria"]} == {
        "operating_days",
        "max_drawdown",
        "profit_factor",
        "daily_loss",
    }


def test_report_cli_prints_ineligible_instead_of_approved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def fake_collect(*_: Any, **__: Any) -> Any:
        return _gate_passing_inputs(allow_open_calendar=True)

    monkeypatch.setattr(paper, "collect_inputs", fake_collect)
    monkeypatch.setattr(
        paper, "load_settings", lambda: SimpleNamespace(database_url="postgresql+asyncpg://u:p@127.0.0.1:1/x")
    )
    args = paper.build_parser().parse_args(
        ["report", "--run-id", "r1", "--no-compare", "--out", str(tmp_path), "--date", "2026-08-01"]
    )

    assert asyncio.run(paper.run_report(args)) == paper.EXIT_OK

    out = capsys.readouterr().out
    assert "Live INELEGIVEL: calendario aberto" in out and "APROVADO" not in out
