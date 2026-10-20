from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from engine.application.risk import RiskState
from engine.domain.journal import ControlAction
from engine.domain.order import OrderStatus
from engine.domain.risk import RiskConfig
from engine.infrastructure.paper import PaperGateway
from engine.runner.paper_control import Command, CommandName, CommandQueue
from engine.runner.paper_robot import BotState, RunnerInvariantError
from tests.unit.backtest.engine_helpers import T0
from tests.unit.runner.fakes import (
    FakeFeed,
    Harness,
    SimulatedCrash,
    candle_at,
    config,
    warmup,
)

D = Decimal
SIGNAL_CANDLE = ("100", "100.5", "99.5", "100")
FILL_CANDLE = ("100", "101", "99.5", "100.5")
QUIET = ("100", "100.1", "99.9", "100")
STOP_CANDLE = ("100.5", "100.6", "94", "95")
ENTRY_29 = "e2601020600BTC"


@pytest.fixture(autouse=True)
def cancel_spy(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Registra todo cancel_order do gateway: o OCO (stop) NUNCA pode ser cancelado em operacao normal."""
    calls: list[str] = []
    original = PaperGateway.cancel_order

    async def spy(self: PaperGateway, symbol: str, client_order_id: str):  # type: ignore[no-untyped-def]
        calls.append(client_order_id)
        return await original(self, symbol, client_order_id)

    monkeypatch.setattr(PaperGateway, "cancel_order", spy)
    yield calls
    assert not [c for c in calls if c.endswith(":oco")], f"OCO cancelado: {calls}"


async def entered(**kwargs: object) -> Harness:
    """Sinal na vela 29, fill por toque na vela 30: posicao aberta com OCO e parcial no gateway."""
    h = await Harness(**kwargs).start()  # type: ignore[arg-type]
    await h.close(candle_at(29, *SIGNAL_CANDLE))
    await h.close(candle_at(30, *FILL_CANDLE))
    return h


async def test_approved_signal_becomes_order_then_stop_protected_position() -> None:
    h = await Harness().start()
    await h.close(candle_at(29, *SIGNAL_CANDLE))

    assert h.journal.codes() == ["approved"]
    order = h.journal.orders[ENTRY_29]
    assert order.status is OrderStatus.NEW and order.stop_loss == D(95)

    await h.close(candle_at(30, *FILL_CANDLE))

    assert h.journal.orders[ENTRY_29].status is OrderStatus.FILLED
    assert h.journal.orders[f"{ENTRY_29}:oco"].status is OrderStatus.NEW
    assert h.journal.orders[f"{ENTRY_29}:partial"].status is OrderStatus.NEW
    position = h.journal.positions["BTC/USDT"]
    assert position.stop == D(95) and position.entry_price == D(100)
    [oco] = h.robot.gateway.open_protective_state()
    assert oco.stop == position.stop and oco.target is None
    assert h.robot.live_symbols == ["BTC/USDT"]
    assert len(h.journal.fills) == 1
    assert {"decisions", "orders", "equity"} <= {kind for kind, _ in h.bus.events}


async def test_every_evaluated_candle_records_a_decision_even_without_signal() -> None:
    h = await Harness(strategy_indexes=set()).start()
    for i in range(29, 32):
        await h.close(candle_at(i, *QUIET))

    assert h.journal.codes() == ["no_signal"] * 3
    assert [d.decided_at for d in h.journal.decisions] == [T0 + timedelta(hours=i + 1) for i in range(29, 32)]
    assert all(not d.traded for d in h.journal.decisions)


async def test_history_gap_does_not_trade_and_records_why() -> None:
    h = await Harness().start()
    h.feed.fail_fetch = True
    await h.close(candle_at(30, *SIGNAL_CANDLE))

    assert h.journal.codes()[-1] == "history_gap"
    assert h.journal.orders == {}


async def test_gap_is_recovered_through_fetch_candles_and_trading_resumes() -> None:
    skipped = candle_at(29, *QUIET)
    h = await Harness(strategy_indexes={30}, feed=FakeFeed([*warmup(), skipped])).start()
    h.feed.candles = [*warmup(), skipped]
    await h.close(candle_at(30, *SIGNAL_CANDLE))

    assert "approved" in h.journal.codes()
    assert "catch_up" in h.journal.codes()


async def test_expired_approval_fill_is_an_emergency_exit_sold_at_market() -> None:
    h = await Harness().start()
    await h.close(candle_at(29, *SIGNAL_CANDLE))
    for i in (30, 31):
        await h.close(candle_at(i, "101", "102", "100.5", "101"))
    await h.close(candle_at(32, "101", "101.5", "99.5", "100.2"))

    assert "emergency_exit" in h.journal.codes()
    assert h.robot.live_symbols == ["BTC/USDT"] and h.journal.positions == {}
    h.clock.advance(seconds=5)
    await h.tick("100.1", "100.12")

    [trade] = h.journal.trades
    assert trade.exit_reason.value == "manual" and trade.trade_id == f"{ENTRY_29}:emg1"[:-1]
    assert h.robot.live_symbols == []
    assert h.bus.of("alerts")[0]["alert"] == "emergency_exit"
    assert h.robot.state.ledger == (trade,)


async def test_gate_rejection_becomes_a_decision_without_order() -> None:
    h = await Harness(cfg=config(allow_open_calendar=False)).start()
    await h.close(candle_at(29, *SIGNAL_CANDLE))

    [decision] = h.journal.decisions
    assert (decision.code, decision.traded) == ("event_calendar_stale", False)
    assert h.journal.orders == {}


async def test_stop_loss_closes_the_position_and_feeds_the_risk_state() -> None:
    h = await entered()
    await h.close(candle_at(31, *STOP_CANDLE))

    [trade] = h.journal.trades
    assert trade.exit_reason.value == "stop_loss" and trade.pnl < 0
    assert h.journal.positions == {} and h.robot.live_symbols == []
    assert h.robot.state.ledger == (trade,)
    assert h.robot.state.equity == D(10_000) + trade.pnl
    assert h.journal.orders[f"{ENTRY_29}:oco"].status is OrderStatus.FILLED
    assert h.journal.orders[f"{ENTRY_29}:partial"].status is OrderStatus.CANCELED
    assert h.journal.equity[-1].in_position is False


async def test_two_losses_on_the_pair_block_new_entries() -> None:
    h = await Harness(strategy_indexes={29, 33, 37}).start()
    for first in (29, 33):
        await h.close(candle_at(first, *SIGNAL_CANDLE))
        await h.close(candle_at(first + 1, *FILL_CANDLE))
        await h.close(candle_at(first + 2, *STOP_CANDLE))
        await h.close(candle_at(first + 3, *QUIET))
    await h.close(candle_at(37, *SIGNAL_CANDLE))

    assert len(h.journal.trades) == 2
    assert h.journal.decisions[-1].code in {"daily_loss_pause", "pair_paused"}
    assert [o for o in h.journal.orders if ":" not in o] == [ENTRY_29, "e2601021000BTC"]


async def test_drawdown_halts_the_robot_until_manual_reactivation(tmp_path: Path) -> None:
    queue = CommandQueue(tmp_path)
    h = await entered()
    h.robot.commands = queue
    h.robot.state = h.robot.state.model_copy(update={"peak_equity": D(10_560)})
    await h.close(candle_at(31, *STOP_CANDLE))

    assert h.robot.state.halted and h.robot.bot_state is BotState.HALTED
    assert ControlAction.HALT in {e.action for e in h.journal.controls}
    await h.close(candle_at(32, *QUIET))
    queue.send(Command(name=CommandName.RESUME, operator="ana", reason="tentativa", issued_at=h.clock.now()))
    await h.robot.housekeeping()
    assert h.robot.state.halted
    queue.send(
        Command(name=CommandName.REACTIVATE, operator="ana", reason="revisado", issued_at=h.clock.now())
    )
    await h.robot.housekeeping()

    assert not h.robot.state.halted and h.robot.bot_state is BotState.RUNNING
    event = h.journal.controls[-1]
    assert (event.action, event.operator, event.reason) == (ControlAction.REACTIVATE, "ana", "revisado")
    assert h.robot.state.audit[-1].operator == "ana"


async def test_stops_blind_pauses_entries_alerts_and_recovers() -> None:
    h = await entered()
    h.clock.advance(seconds=31)
    await h.robot.housekeeping()

    assert h.robot.bot_state is BotState.PAUSED and h.robot.gateway.stops_blind
    assert h.bus.of("alerts")[-1]["alert"] == "stops_blind"
    await h.close(candle_at(31, *QUIET), bid="99.99", ask="100.01")
    h.clock.advance(seconds=1)
    await h.tick()
    await h.robot.housekeeping()

    assert h.robot.bot_state is BotState.RUNNING
    actions = [e.action for e in h.journal.controls]
    assert actions[-2:] == [ControlAction.PAUSE, ControlAction.RESUME]


async def test_stops_blind_with_signal_records_the_reason() -> None:
    h = await Harness(strategy_indexes={29, 31}).start()
    await h.close(candle_at(29, *SIGNAL_CANDLE))
    await h.close(candle_at(30, *FILL_CANDLE))
    h.clock.advance(seconds=3000)
    await h.robot.housekeeping()
    h.clock.set(candle_at(31, *QUIET).close_time)
    await h.robot.on_candle(candle_at(31, *QUIET))
    assert h.robot.gateway.stops_blind is False  # candle chegou: dado fresco
    assert h.journal.decisions[-1].code in {"stops_blind", "position_already_open", "no_quote"}


async def test_reconcile_divergence_pauses_and_never_lowers_a_stop() -> None:
    h = await entered()
    stored = h.journal.positions["BTC/USDT"]
    h.journal.positions["BTC/USDT"] = stored.model_copy(update={"stop": D(96)})
    h.clock.advance(seconds=301)
    await h.tick()
    await h.robot.housekeeping()

    assert h.robot.state.paused and "reconciliacao" in (h.robot.state.pause_reason or "")
    assert h.journal.controls[-1].action is ControlAction.PAUSE
    assert h.bus.of("alerts")[-1]["alert"] == "reconcile_divergence"
    [oco] = h.robot.gateway.open_protective_state()
    assert oco.stop == D(95)


async def test_position_without_active_stop_in_gateway_pauses() -> None:
    h = await entered()
    h.robot.gateway._ocos.clear()
    h.clock.advance(seconds=301)
    await h.tick()
    await h.robot.housekeeping()

    assert h.robot.state.paused
    assert "sem stop ativo" in (h.robot.state.pause_reason or "")


async def test_clean_shutdown_keeps_stops_and_saves_state() -> None:
    h = await Harness(strategy_indexes={29, 31}).start()
    await h.close(candle_at(29, *SIGNAL_CANDLE))
    await h.close(candle_at(30, *FILL_CANDLE))
    await h.close(candle_at(31, *SIGNAL_CANDLE))
    pending_before = set(h.robot._pending)
    await h.robot.shutdown("teste")

    assert h.robot.bot_state is BotState.STOPPED
    assert h.journal.controls[-1].action is ControlAction.STOP
    [oco] = h.robot.gateway.open_protective_state()
    assert oco.stop == D(95) and oco.status is OrderStatus.NEW
    saved = h.journal.gateway_states[-1]["gateway"]["ocos"]
    assert [o["status"] for o in saved] == ["new"]
    assert not pending_before or all(
        h.journal.orders[i].status is OrderStatus.CANCELED for i in pending_before
    )


async def test_oco_can_never_be_cancelled_by_the_robot() -> None:
    h = await entered()
    with pytest.raises(RunnerInvariantError):
        await h.robot._cancel_order("BTC/USDT", f"{ENTRY_29}:oco")


async def test_emergency_stop_blocks_entries_and_can_liquidate_as_manual(tmp_path: Path) -> None:
    queue = CommandQueue(tmp_path)
    h = await entered()
    h.robot.commands = queue
    queue.send(
        Command(
            name=CommandName.EMERGENCY_STOP,
            operator="ana",
            reason="incidente",
            liquidate=True,
            issued_at=h.clock.now(),
        )
    )
    await h.robot.housekeeping()

    assert h.robot.state.paused and "emergency-stop" in (h.robot.state.pause_reason or "")
    h.clock.advance(seconds=5)
    await h.tick("100.4", "100.5")
    await h.robot.housekeeping()
    [trade] = h.journal.trades
    assert trade.exit_reason.value == "manual" and h.robot.live_symbols == []
    assert h.journal.positions == {}


async def test_emergency_stop_without_liquidation_keeps_position_protected(tmp_path: Path) -> None:
    queue = CommandQueue(tmp_path)
    h = await entered()
    h.robot.commands = queue
    queue.send(
        Command(name=CommandName.EMERGENCY_STOP, operator="ana", reason="incidente", issued_at=h.clock.now())
    )
    await h.robot.housekeeping()

    assert h.robot.state.paused and h.robot.live_symbols == ["BTC/USDT"]
    assert h.robot.gateway.open_protective_state()


async def test_stop_command_requests_graceful_stop(tmp_path: Path) -> None:
    queue = CommandQueue(tmp_path)
    h = await Harness().start()
    h.robot.commands = queue
    queue.send(Command(name=CommandName.STOP, operator="ana", reason="fim do dia", issued_at=h.clock.now()))
    await h.robot.housekeeping()

    assert h.robot.stop_event.is_set() and queue.pending() == []


async def test_pause_and_resume_commands_are_audited(tmp_path: Path) -> None:
    queue = CommandQueue(tmp_path)
    h = await Harness().start()
    h.robot.commands = queue
    queue.send(Command(name=CommandName.PAUSE, operator="ana", reason="manutencao", issued_at=h.clock.now()))
    await h.robot.housekeeping()
    assert h.robot.bot_state is BotState.PAUSED
    h.clock.advance(seconds=1)
    queue.send(Command(name=CommandName.RESUME, operator="ana", reason="ok", issued_at=h.clock.now()))
    await h.robot.housekeeping()

    assert h.robot.bot_state is BotState.RUNNING
    assert [e.action for e in h.journal.controls][-2:] == [ControlAction.PAUSE, ControlAction.RESUME]
    assert h.robot.state.audit[-1].operator == "ana"


async def test_deterministic_with_a_fake_clock() -> None:
    async def run() -> tuple[list[tuple[str, str]], list[str], list[str]]:
        h = await Harness(strategy_indexes={29, 33}).start()
        for i, row in enumerate(
            (SIGNAL_CANDLE, FILL_CANDLE, STOP_CANDLE, QUIET, SIGNAL_CANDLE, FILL_CANDLE), 29
        ):
            await h.close(candle_at(i, *row))
        return (
            [(d.decided_at.isoformat(), d.code) for d in h.journal.decisions],
            [t.model_dump_json() for t in h.journal.trades],
            [p.model_dump_json() for p in h.journal.equity],
        )

    assert await run() == await run()


# ---------------------------------------------------------------- recuperacao apos queda


async def restart(h: Harness, *, candles: list | None = None, **kwargs: object) -> Harness:
    """Novo processo sobre o mesmo diario (e o mesmo mercado), como depois de um kill."""
    feed = FakeFeed(candles if candles is not None else h.feed.candles)
    revived = Harness(journal=h.journal, history=h.history, feed=feed, clock=h.clock, **kwargs)  # type: ignore[arg-type]
    return await revived.start()


async def test_recovery_before_the_fill_cancels_the_stale_pending_entry() -> None:
    h = await Harness().start()
    await h.close(candle_at(29, *SIGNAL_CANDLE))
    h.clock.advance(seconds=600)
    revived = await restart(h)

    assert h.journal.orders[ENTRY_29].status is OrderStatus.CANCELED
    assert "entry_cancelled_on_restart" in h.journal.codes()
    assert revived.robot.live_symbols == [] and not revived.robot.state.paused
    assert revived.robot.gateway.snapshot().orders == ()


async def test_recovery_between_fill_and_persistence_loses_nothing_consistent() -> None:
    h = await Harness().start()
    await h.close(candle_at(29, *SIGNAL_CANDLE))
    h.journal.crash_on = {"record_entry"}
    with pytest.raises(SimulatedCrash):
        await h.close(candle_at(30, *FILL_CANDLE))
    h.journal.crash_on = set()
    revived = await restart(h)

    assert h.journal.positions == {} and h.journal.fills == {}
    assert revived.robot.live_symbols == [] and not revived.robot.state.paused
    assert revived.robot.gateway.positions == {}


async def test_recovery_with_open_position_keeps_the_stop_and_state() -> None:
    h = await entered()
    revived = await restart(h)

    assert revived.robot.live_symbols == ["BTC/USDT"]
    [oco] = revived.robot.gateway.open_protective_state()
    assert oco.stop == D(95)
    assert not revived.robot.state.paused
    assert revived.robot.state.equity == h.robot.state.equity


async def test_recovery_executes_a_stop_crossed_while_the_robot_was_down() -> None:
    h = await entered()
    down = [candle_at(31, "100.5", "100.6", "94", "95"), candle_at(32, "95", "96", "94.5", "95.5")]
    h.clock.set(down[-1].close_time + timedelta(seconds=30))
    revived = await restart(
        h, candles=[*h.history, candle_at(29, *SIGNAL_CANDLE), candle_at(30, *FILL_CANDLE), *down]
    )

    [trade] = h.journal.trades
    assert trade.exit_reason.value == "stop_loss"
    assert revived.robot.live_symbols == [] and h.journal.positions == {}
    assert revived.robot.state.ledger == (trade,)
    assert not revived.robot.state.paused


async def test_halted_stays_halted_after_restart() -> None:
    h = await entered()
    h.robot.state = h.robot.state.model_copy(update={"peak_equity": D(10_560)})
    await h.close(candle_at(31, *STOP_CANDLE))
    assert h.robot.state.halted
    revived = await restart(h)

    assert revived.robot.state.halted and revived.robot.bot_state is BotState.HALTED


async def test_restart_with_manual_pause_stays_paused() -> None:
    h = await entered()
    await h.robot._pause("teste", operator="ana")
    revived = await restart(h)

    assert revived.robot.state.paused and revived.robot.state.pause_reason == "teste"


async def test_risk_state_roundtrips_through_the_snapshot() -> None:
    h = await entered()
    saved = RiskState.model_validate_json(h.robot.state.model_dump_json())
    assert saved == h.robot.state
    assert RiskConfig().max_drawdown == D("0.05")
