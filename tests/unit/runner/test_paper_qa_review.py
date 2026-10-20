"""Revisao QA do runner de paper: falhas de infraestrutura e honestidade do gate.

Os testes `test_BUG_*` nasceram como `xfail(strict=True)` documentando defeitos reais; hoje sao regressoes.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from engine.application.backtest.models import EquityPoint
from engine.domain.journal import ControlAction, ControlEvent
from engine.domain.risk import RiskConfig
from engine.domain.trade import ExitReason, Trade
from engine.domain.types import Timeframe
from engine.runner.paper_config import RunPaths
from engine.runner.paper_control import Command, CommandName, CommandQueue
from engine.runner.paper_outbox import PersistenceUnavailableError
from engine.runner.paper_report import DecisionSummary, ReportInputs, build_cumulative
from engine.runner.paper_robot import BotState
from tests.unit.runner.fakes import Harness, MemoryJournal, SimulatedCrash, candle_at
from tests.unit.runner.test_paper_robot import (
    ENTRY_29,
    FILL_CANDLE,
    QUIET,
    SIGNAL_CANDLE,
    STOP_CANDLE,
    entered,
    restart,
)

D = Decimal


class OutageJournal(MemoryJournal):
    """Diario cujo banco 'cai': o driver real levanta OSError/TimeoutError.

    Por padrao cai uma unica vez por metodo; com `persistent` o metodo falha ate `outage` ser esvaziado.
    """

    def __init__(self) -> None:
        super().__init__()
        self.outage: set[str] = set()
        self.persistent = False

    def _enter(self, name: str) -> None:
        if name in self.outage:
            if not self.persistent:
                self.outage.discard(name)
            self.calls.append(name)
            raise ConnectionRefusedError("banco indisponivel")
        super()._enter(name)


async def _swallowed_by_supervisor(h: Harness, action: Any) -> bool:
    """Roda `action` sob o supervisor dos feeds; True se o erro foi engolido como 'feed caiu'."""

    async def factory() -> None:
        await action()
        await asyncio.sleep(3600)

    task = asyncio.create_task(h.robot._supervise("candles:BTC/USDT", factory))
    await asyncio.sleep(0.2)
    swallowed = not task.done()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    return swallowed


async def _entry_pending_then_outage_on_fill() -> tuple[Harness, bool]:
    h = await Harness(journal=OutageJournal()).start()
    await h.close(candle_at(29, *SIGNAL_CANDLE))
    candle = candle_at(30, *FILL_CANDLE)
    h.clock.set(candle.close_time)
    h.journal.outage = {"record_entry"}
    swallowed = await _swallowed_by_supervisor(h, lambda: h.robot.on_candle(candle))
    return h, swallowed


async def test_database_outage_during_entry_fill_must_not_leave_an_unpersisted_position() -> None:
    h, swallowed = await _entry_pending_then_outage_on_fill()

    assert swallowed
    assert "BTC/USDT" in h.journal.positions
    assert ENTRY_29 in {f.client_order_id for f in h.journal.fills.values()}
    assert len(h.robot.journal.outbox) == 0 and not h.robot.state.paused


async def test_outage_during_entry_fill_is_queued_in_the_outbox_and_reapplied_after_restart(
    tmp_path: Path,
) -> None:
    paths = RunPaths(tmp_path, "test-run")
    h = await Harness(journal=OutageJournal(), paths=paths).start()
    await h.close(candle_at(29, *SIGNAL_CANDLE))
    candle = candle_at(30, *FILL_CANDLE)
    h.clock.set(candle.close_time)
    h.journal.outage, h.journal.persistent = {"record_entry"}, True
    await h.robot.on_candle(candle)  # a queda e absorvida: o evento vai para o outbox, nunca se perde
    assert h.robot.bot_state is BotState.FAILED_PERSISTENCE
    assert h.robot.state.paused and len(h.robot.journal.outbox) >= 1
    await h.close(candle_at(31, *QUIET))  # o salvamento periodico nao pode gravar o gateway com o OCO ativo
    assert not h.journal.fills  # o fill ainda nao chegou ao diario
    assert h.robot._seen_seq >= 1  # ... mas esta no outbox duravel: o fill foi tratado

    h.journal.outage, h.journal.persistent = set(), False
    revived = await restart(h, paths=paths)
    await revived.close(candle_at(32, *STOP_CANDLE))

    assert [t.exit_reason.value for t in h.journal.trades] == ["stop_loss"]
    assert revived.robot.live_symbols == []
    assert h.journal.orders[ENTRY_29].status.value == "filled"
    assert any(f.client_order_id == ENTRY_29 for f in h.journal.fills.values())
    assert len(revived.robot.journal.outbox) == 0 and not revived.robot.state.paused


async def test_outbox_is_reapplied_by_housekeeping_when_the_database_comes_back(tmp_path: Path) -> None:
    h = await Harness(journal=OutageJournal(), paths=RunPaths(tmp_path, "test-run")).start()
    await h.close(candle_at(29, *SIGNAL_CANDLE))
    candle = candle_at(30, *FILL_CANDLE)
    h.clock.set(candle.close_time)
    h.journal.outage, h.journal.persistent = {"record_entry"}, True
    await h.robot.on_candle(candle)
    assert h.robot.bot_state is BotState.FAILED_PERSISTENCE and not h.journal.positions

    h.journal.outage = set()
    await h.robot.housekeeping()

    assert "BTC/USDT" in h.journal.positions and ENTRY_29 in {
        f.client_order_id for f in h.journal.fills.values()
    }
    assert h.robot.bot_state is BotState.RUNNING and len(h.robot.journal.outbox) == 0
    alerts = [a["alert"] for a in h.bus.of("alerts")]
    assert alerts.index("persistence_down") < alerts.index("persistence_recovered")


async def test_start_refuses_to_run_while_the_outbox_cannot_be_applied(tmp_path: Path) -> None:
    paths = RunPaths(tmp_path, "test-run")
    h = await Harness(journal=OutageJournal(), paths=paths).start()
    await h.close(candle_at(29, *SIGNAL_CANDLE))
    candle = candle_at(30, *FILL_CANDLE)
    h.clock.set(candle.close_time)
    h.journal.outage, h.journal.persistent = {"record_entry"}, True
    await h.robot.on_candle(candle)

    with pytest.raises(PersistenceUnavailableError):
        await restart(h, paths=paths)


async def test_BUG_crash_between_halt_event_and_trade_row_must_not_lose_the_loss() -> None:
    h = await Harness().start()
    await h.close(candle_at(29, *SIGNAL_CANDLE))
    await h.close(candle_at(30, *FILL_CANDLE))
    h.robot.state = h.robot.state.model_copy(update={"peak_equity": D(10_560)})
    h.journal.crash_on = {"record_exit"}
    with pytest.raises(SimulatedCrash):
        await h.close(candle_at(31, *STOP_CANDLE))
    h.journal.crash_on = set()

    revived = await restart(
        h, candles=[*h.history, candle_at(29, *SIGNAL_CANDLE), candle_at(30, *FILL_CANDLE)]
    )

    ledger = {t.trade_id for t in revived.robot.state.ledger}
    stored = {t.trade_id for t in h.journal.trades}
    assert ledger <= stored, f"perda no ledger do risco mas ausente do diario de trades: {ledger - stored}"


# ---------------------------------------------------------------- trava do Live


def _gate_passing_inputs(**overrides: Any) -> ReportInputs:
    start = datetime(2026, 7, 1, tzinfo=UTC)
    now = start + timedelta(days=70)
    trades = []
    for i in range(40):
        win = i % 6 != 0
        opened = start + timedelta(days=i + 1, hours=1)
        trades.append(
            Trade(
                trade_id=f"t{i}",
                symbol="BTC/USDT",
                quantity=D(1),
                entry_price=D(100),
                exit_price=D(103) if win else D(99),
                opened_at=opened,
                closed_at=opened + timedelta(hours=2),
                exit_reason=ExitReason.TAKE_PROFIT if win else ExitReason.STOP_LOSS,
            )
        )
    curve = [
        EquityPoint(
            at=start + timedelta(minutes=30 * i), equity=D(10_000), realized=D(10_000), in_position=False
        )
        for i in range(70 * 48)
    ]
    values: dict[str, Any] = {
        "run_id": "r1",
        "capital": D(10_000),
        "risk": RiskConfig(),
        "timeframe": Timeframe.H1,
        "symbols": ("BTC/USDT",),
        "strategies": ("elephant",),
        "trades": trades,
        "curve": curve,
        "events": [ControlEvent(at=start, action=ControlAction.START, operator="system", reason="inicio")],
        "decisions": [DecisionSummary(start, "BTC/USDT", "no_signal", False)],
        "now": now,
    }
    return ReportInputs(**(values | overrides))


def test_gate_baseline_can_approve_a_clean_run() -> None:
    report = build_cumulative(_gate_passing_inputs())

    assert report["live_gate"]["approved"] is True


def test_BUG_open_calendar_run_must_not_be_approved_for_live() -> None:
    report = build_cumulative(_gate_passing_inputs(allow_open_calendar=True))

    assert report["live_gate"]["approved"] is False


async def test_BUG_operator_command_survives_a_failed_execution(tmp_path: Path) -> None:
    h = await Harness().start()
    queue = CommandQueue(tmp_path)
    h.robot.commands = queue
    queue.send(
        Command(name=CommandName.EMERGENCY_STOP, operator="ana", reason="incidente", issued_at=h.clock.now())
    )
    h.journal.crash_on = {"record_control"}
    with pytest.raises(SimulatedCrash):
        await h.robot.housekeeping()
    h.journal.crash_on = set()

    assert [c.name for _, c in queue.pending()] == [CommandName.EMERGENCY_STOP]


async def test_BUG_market_exit_leaves_the_oco_order_row_open_in_the_journal() -> None:
    h = await entered()
    await h.close(candle_at(31, "100.5", "108", "100.4", "104"))
    h.clock.advance(seconds=1)
    await h.tick("103.9", "104.1")

    assert h.robot.live_symbols == []
    assert h.journal.orders[f"{ENTRY_29}:oco"].status.value in {"canceled", "filled"}


# ---------------------------------------------------------------- regras criticas (matam mutantes)


async def test_forged_approval_never_reaches_the_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    from engine.application.risk import ApprovalError
    from engine.runner import paper_robot

    real = paper_robot.evaluate_entry

    def tampered(*args: Any, **kwargs: Any) -> Any:
        outcome = real(*args, **kwargs)
        object.__setattr__(outcome, "risk_amount", D(1_000_000))
        return outcome

    monkeypatch.setattr(paper_robot, "evaluate_entry", tampered)
    h = await Harness().start()

    with pytest.raises(ApprovalError):
        await h.close(candle_at(29, *SIGNAL_CANDLE))

    assert h.robot.gateway.submitted_orders == []


async def test_expired_entry_is_cancelled_every_cycle_without_touching_any_oco() -> None:
    h = await Harness().start()
    await h.close(candle_at(29, *SIGNAL_CANDLE))
    expires = h.robot._pending[ENTRY_29].approved.guards.expires_at
    h.clock.set(expires + timedelta(seconds=1))

    await h.tick("101.50", "101.60")  # sem tocar o limite: a unica saida e o vencimento
    await h.robot.housekeeping()

    assert h.journal.orders[ENTRY_29].status.value == "canceled"
    assert "entry_expired" in h.journal.codes()
    assert h.robot._pending == {}
    assert h.robot.gateway.snapshot(compact=False).ocos == ()


async def test_BUG_status_file_locked_by_a_reader_must_not_kill_housekeeping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = await Harness().start()
    h.robot.paths = RunPaths(tmp_path, "test-run")

    def locked(*_: Any, **__: Any) -> None:
        raise PermissionError(13, "arquivo em uso por outro processo")

    monkeypatch.setattr("engine.runner.paper_control.os.replace", locked)
    await h.tick()

    await h.robot.housekeeping()


async def test_BUG_swallowed_outage_on_a_stop_exit_must_not_hide_the_loss_from_the_trades_table() -> None:
    h = await Harness(journal=OutageJournal()).start()
    await h.close(candle_at(29, *SIGNAL_CANDLE))
    await h.close(candle_at(30, *FILL_CANDLE))
    stop_candle = candle_at(31, *STOP_CANDLE)
    h.clock.set(stop_candle.close_time)
    h.journal.outage = {"record_exit"}
    swallowed = await _swallowed_by_supervisor(h, lambda: h.robot.on_candle(stop_candle))
    await h.close(candle_at(32, *QUIET))

    assert swallowed
    assert len(h.journal.trades) == len(h.robot.state.ledger) == 1


async def test_emergency_exit_pending_at_a_crash_is_still_sold_after_the_restart() -> None:
    h = await Harness().start()
    await h.close(candle_at(29, *SIGNAL_CANDLE))
    for i in (30, 31):
        await h.close(candle_at(i, "101", "102", "100.5", "101"))
    await h.close(candle_at(32, "101", "101.5", "99.5", "100.2"))
    assert "emergency_exit" in h.journal.codes() and h.robot.live_symbols == ["BTC/USDT"]

    revived = await restart(
        h, candles=[*h.history, *[candle_at(i, "101", "102", "100.5", "101") for i in (30, 31)]]
    )
    revived.clock.advance(seconds=5)
    await revived.tick("100.1", "100.12")

    assert revived.robot.live_symbols == []
    assert [t.exit_reason.value for t in h.journal.trades] == ["manual"]


async def test_candle_that_is_still_open_is_never_used_for_a_decision() -> None:
    h = await Harness().start()
    candle = candle_at(29, *SIGNAL_CANDLE).model_copy(update={"closed": False})
    h.clock.set(candle.close_time)
    await h.tick()

    await h.robot.on_candle(candle)

    assert h.journal.decisions == []
    assert h.robot.gateway.submitted_orders == []
    assert h.robot._last_handled.get("BTC/USDT") != candle.open_time


async def _expired_fill_emergency() -> Harness:
    h = await Harness(strategy_indexes={29, 33}).start()
    await h.close(candle_at(29, *SIGNAL_CANDLE))
    for i in (30, 31):
        await h.close(candle_at(i, "101", "102", "100.5", "101"))
    await h.close(candle_at(32, "101", "101.5", "99.5", "100.2"))
    return h


async def test_emergency_exit_is_sold_in_the_same_cycle_when_the_book_is_fresh() -> None:
    h = await _expired_fill_emergency()

    assert f"{ENTRY_29}:emg1" in h.journal.orders


async def test_no_new_entry_while_an_emergency_sale_is_pending() -> None:
    h = await _expired_fill_emergency()
    candle = candle_at(33, "100.2", "100.6", "100", "100.4")
    h.clock.set(candle.close_time)  # sem book novo: a venda de emergencia segue pendente

    await h.robot.on_candle(candle)

    assert h.robot.live_symbols == ["BTC/USDT"]
    assert h.journal.codes()[-1] == "emergency_exit_pending"


async def test_reactivate_without_a_halt_is_refused_and_does_not_crash(tmp_path: Path) -> None:
    h = await Harness().start()
    queue = CommandQueue(tmp_path)
    h.robot.commands = queue
    queue.send(Command(name=CommandName.REACTIVATE, operator="ana", reason="teste", issued_at=h.clock.now()))

    await h.robot.housekeeping()

    assert not h.robot.state.halted and queue.pending() == []
    assert h.journal.controls[-1].action is not ControlAction.REACTIVATE


async def test_capital_divergence_between_gateway_and_ledger_pauses_the_robot() -> None:
    h = await Harness().start()
    h.robot.gateway._balances["USDT"] += D(5)

    await h.close(candle_at(29, *QUIET))

    assert h.robot.state.paused and "capital" in (h.robot.state.pause_reason or "")


async def test_candle_delivered_twice_is_evaluated_once() -> None:
    h = await Harness().start()
    candle = candle_at(29, *SIGNAL_CANDLE)
    await h.close(candle)

    await h.robot.on_candle(candle)

    assert len(h.strategy.seen) == 1
    assert h.journal.codes() == ["approved"]


async def test_entry_without_a_fresh_quote_is_skipped_not_traded() -> None:
    h = await Harness(quotes=lambda _: None).start()

    await h.close(candle_at(29, *SIGNAL_CANDLE))

    assert h.journal.codes() == ["no_quote"]
    assert h.robot.gateway.submitted_orders == []
