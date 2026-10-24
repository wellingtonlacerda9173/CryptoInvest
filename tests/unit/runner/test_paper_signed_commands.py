"""Robo + canal assinado: comandos da API chegam, sao verificados, aplicados e confirmados."""

import asyncio
import json
from collections.abc import Mapping
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from engine.infrastructure.notifications import AlertDispatcher, NotifyEvent
from engine.runner.command_channel import RedisCommandSource, sign_command
from engine.runner.paper_control import Command, CommandName, CommandQueue
from engine.runner.paper_robot import BotState
from tests.redis_fakes import FakeRedis
from tests.unit.runner.fakes import Harness

KEY = b"k" * 48


class AuditRecorder:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str, Mapping[str, Any]]] = []

    async def audit(self, actor: str, action: str, entity: str, payload: Mapping[str, Any]) -> None:
        self.rows.append((actor, action, entity, payload))

    def actions(self) -> list[str]:
        return [row[1] for row in self.rows]


class Setup:
    def __init__(self, harness: Harness, redis: FakeRedis, tmp: Path) -> None:
        self.h = harness
        self.redis = redis
        self.source = RedisCommandSource(redis, KEY)
        self.audit = AuditRecorder()
        harness.robot.commands = CommandQueue(tmp)
        harness.robot.signed_commands = self.source
        harness.robot.audit_sink = self.audit
        self.counter = 0

    async def send(self, name: CommandName, *, key: bytes = KEY, **fields: Any) -> str:
        self.counter += 1
        command_id = f"cmd{self.counter:012d}"
        command = Command(
            name=name,
            operator=fields.pop("operator", "ana (ana.silva)"),
            reason=fields.pop("reason", "teste"),
            issued_at=fields.pop("issued_at", self.h.clock.now()),
            **fields,
        )
        await self.redis.rpush(
            self.source._keys.commands,
            sign_command(key, command, command_id=command_id, signed_at=self.h.clock.now()),
        )
        return command_id

    async def ack(self, command_id: str) -> dict[str, Any]:
        raw = await self.redis.get(self.source._keys.ack(command_id))
        assert raw is not None, "sem confirmacao"
        return json.loads(raw)  # type: ignore[no-any-return]


@pytest.fixture
async def setup(fake_redis: FakeRedis, tmp_path: Path) -> Setup:
    return Setup(await Harness().start(), fake_redis, tmp_path)


async def test_signed_pause_reaches_the_robot_and_is_confirmed(setup: Setup) -> None:
    command_id = await setup.send(CommandName.PAUSE, reason="manutencao")
    await setup.h.robot.housekeeping()

    assert setup.h.robot.bot_state is BotState.PAUSED
    assert (await setup.ack(command_id))["result"] == "applied"
    assert setup.h.robot.state.pause_reason == "manutencao"
    assert setup.audit.actions() == []


async def test_bad_signature_is_discarded_audited_and_never_applied(setup: Setup) -> None:
    command_id = await setup.send(CommandName.PAUSE, key=b"x" * 48)
    await setup.h.robot.housekeeping()

    assert setup.h.robot.bot_state is BotState.RUNNING
    assert setup.audit.actions() == ["paper_command_discarded"]
    payload = setup.audit.rows[0][3]
    assert payload["code"] == "bad_signature" and set(payload) == {"command_id", "code", "digest"}
    assert await setup.redis.get(setup.source._keys.ack(command_id)) is None, (
        "id nao autenticado nunca recebe ack (nao pode pisar no resultado real)"
    )


async def test_replayed_envelope_is_discarded(setup: Setup) -> None:
    command = Command(name=CommandName.PAUSE, operator="ana", reason="x", issued_at=setup.h.clock.now())
    raw = sign_command(KEY, command, command_id="replay0000001", signed_at=setup.h.clock.now())
    await setup.redis.rpush(setup.source._keys.commands, raw, raw)
    await setup.h.robot.housekeeping()

    assert setup.audit.actions() == ["paper_command_discarded"]
    assert setup.audit.rows[0][3]["code"] == "replay"


async def test_stale_envelope_is_discarded(setup: Setup) -> None:
    command = Command(name=CommandName.PAUSE, operator="ana", reason="x", issued_at=setup.h.clock.now())
    raw = sign_command(
        KEY, command, command_id="stale00000001", signed_at=setup.h.clock.now() - timedelta(minutes=5)
    )
    await setup.redis.rpush(setup.source._keys.commands, raw)
    await setup.h.robot.housekeeping()

    assert setup.h.robot.bot_state is BotState.RUNNING
    assert setup.audit.rows[0][3]["code"] == "stale"


async def test_command_issued_before_the_start_is_dropped_but_emergency_stop_is_not(setup: Setup) -> None:
    before = setup.h.robot.started_at - timedelta(minutes=1)
    await setup.send(CommandName.PAUSE, issued_at=before)
    await setup.h.robot.housekeeping()
    assert setup.h.robot.bot_state is BotState.RUNNING

    await setup.send(CommandName.EMERGENCY_STOP, issued_at=before)
    await setup.h.robot.housekeeping()
    assert setup.h.robot.bot_state is BotState.PAUSED
    assert "emergency-stop" in (setup.h.robot.state.pause_reason or "")


async def test_resume_never_reactivates_a_halted_robot(setup: Setup) -> None:
    robot = setup.h.robot
    robot.state = robot.state.model_copy(update={"halted": True, "halt_reason": "drawdown"})
    command_id = await setup.send(CommandName.RESUME)
    await robot.housekeeping()

    assert robot.bot_state is BotState.HALTED
    ack = await setup.ack(command_id)
    assert ack["result"] == "refused" and "drawdown" in ack["detail"]


async def test_apply_config_accepts_only_reductions(setup: Setup) -> None:
    robot = setup.h.robot
    before_hash = robot.config.config_hash()
    reduced = await setup.send(
        CommandName.APPLY_CONFIG, params={"risk": {"risk_per_trade": "0.003", "max_daily_loss": "0.008"}}
    )
    await robot.housekeeping()

    assert robot.risk.risk_per_trade == Decimal("0.003") and robot.risk.max_daily_loss == Decimal("0.008")
    assert robot.config.risk == robot.risk and robot.config.config_hash() != before_hash
    assert (await setup.ack(reduced))["result"] == "applied"
    assert "config_applied" in setup.audit.actions()
    assert robot.status_payload(setup.h.clock.now())["risk"]["risk_per_trade"] == "0.003"


@pytest.mark.parametrize(
    "patch",
    [
        {"risk_per_trade": "0.009"},
        {"taker_fee_rate": "0.002"},
        {"nao_existe": 1},
        {"max_daily_loss": "0.5"},
        {"risk_per_trade": "0.001", "max_daily_loss": "0.0001"},
        {},
    ],
)
async def test_apply_config_refuses_increases_unknown_and_invalid_values(
    setup: Setup, patch: dict[str, Any]
) -> None:
    robot = setup.h.robot
    await setup.send(CommandName.APPLY_CONFIG, params={"risk": {"risk_per_trade": "0.002"}})
    await robot.housekeeping()
    before = robot.risk.model_dump(mode="json")
    await setup.send(CommandName.APPLY_CONFIG, params={"risk": patch})
    await robot.housekeeping()

    assert robot.risk.model_dump(mode="json") == before
    assert "config_apply_refused" in setup.audit.actions()


async def test_apply_config_with_equal_values_changes_nothing(setup: Setup) -> None:
    robot = setup.h.robot
    before = robot.risk
    await setup.send(
        CommandName.APPLY_CONFIG, params={"risk": {"max_open_positions": 2, "max_drawdown": "0.05"}}
    )
    await robot.housekeeping()
    assert robot.risk == before and "config_apply_refused" not in setup.audit.actions()


async def test_apply_config_cannot_be_used_to_raise_a_limit_that_was_lowered(setup: Setup) -> None:
    robot = setup.h.robot
    await setup.send(CommandName.APPLY_CONFIG, params={"risk": {"max_drawdown": "0.03"}})
    await robot.housekeeping()
    assert robot.risk.max_drawdown == Decimal("0.03")
    await setup.send(CommandName.APPLY_CONFIG, params={"risk": {"max_drawdown": "0.05"}})
    await robot.housekeeping()
    assert robot.risk.max_drawdown == Decimal("0.03")


async def test_file_queue_keeps_working_without_the_signed_channel(tmp_path: Path) -> None:
    h = await Harness().start()
    queue = CommandQueue(tmp_path)
    h.robot.commands = queue
    queue.send(Command(name=CommandName.PAUSE, operator="ana", reason="arquivo", issued_at=h.clock.now()))
    await h.robot.housekeeping()
    assert h.robot.bot_state is BotState.PAUSED


async def test_a_broken_signed_source_never_stops_the_robot(tmp_path: Path) -> None:
    class Broken:
        async def poll(self, now: Any) -> list[Any]:
            raise RuntimeError("falha inesperada")

        async def ack(self, command_id: str, result: str, detail: str = "") -> None:
            raise RuntimeError("falha")

    h = await Harness().start()
    h.robot.commands = CommandQueue(tmp_path)
    h.robot.signed_commands = Broken()
    await h.robot.housekeeping()
    assert h.robot.bot_state is BotState.RUNNING


async def test_a_persistently_blind_channel_raises_one_alert_and_recovers_the_counter(tmp_path: Path) -> None:
    class Blind:
        poll_failures = 0

        async def poll(self, now: Any) -> list[Any]:
            self.poll_failures += 1
            return []

    h = await Harness().start()
    h.robot.commands = CommandQueue(tmp_path)
    source = Blind()
    h.robot.signed_commands = source  # type: ignore[assignment]
    alerts: list[str] = []

    async def record(kind: str, message: str) -> None:
        alerts.append(kind)

    h.robot._alert = record  # type: ignore[method-assign]
    for _ in range(12):
        await h.robot.housekeeping()
    assert alerts.count("command_channel_blind") == 1

    source.poll_failures = -1
    await h.robot.housekeeping()
    assert h.robot._signed_blind == 0


class RecordingNotifier:
    def __init__(self, fail: bool = False, delay: float = 0) -> None:
        self.events: list[NotifyEvent] = []
        self.fail, self.delay = fail, delay

    async def notify(self, event: NotifyEvent) -> None:
        await asyncio.sleep(self.delay)
        if self.fail:
            raise OSError("smtp fora do ar")
        self.events.append(event)


async def test_critical_alerts_reach_the_external_notifier() -> None:
    h = await Harness().start()
    notifier = RecordingNotifier()
    h.robot.alerts = AlertDispatcher(notifier, run_id="r1", cooldown_seconds=0)
    await h.robot._alert("halted", "drawdown 5%")
    await h.robot._alert("persistence_down", "banco fora token=abcdefghijklmnopqrstuvwxyz0123456789")
    await h.robot._alert("stops_blind_prolonged", "sem dados ha 6 min")
    await h.robot._alert("command_failed", "irrelevante")
    await h.robot.alerts.drain()

    assert [e.kind for e in notifier.events] == ["halted", "persistence_down", "stops_blind_prolonged"]
    assert all(e.severity == "critical" for e in notifier.events)
    assert "abcdefghijklmnopqrstuvwxyz0123456789" not in notifier.events[1].message


async def test_daily_loss_pause_is_notified_from_the_risk_state() -> None:
    h = await Harness().start()
    notifier = RecordingNotifier()
    h.robot.alerts = AlertDispatcher(notifier, cooldown_seconds=0)
    robot = h.robot
    loss = robot.state.equity * Decimal("0.02")
    new = robot.state.observe_equity(
        robot.state.equity - loss, h.clock.now() + timedelta(seconds=1), robot.risk
    )
    await robot._apply_state(new, h.clock.now())
    await robot.alerts.drain()  # type: ignore[union-attr]

    assert [e.kind for e in notifier.events] == ["daily_loss_pause"]


async def test_a_failing_or_slow_notifier_never_affects_the_robot() -> None:
    h = await Harness().start()
    h.robot.alerts = AlertDispatcher(RecordingNotifier(fail=True), cooldown_seconds=0)
    await h.robot._alert("halted", "x")
    await h.robot.alerts.drain()
    slow = AlertDispatcher(RecordingNotifier(delay=5), cooldown_seconds=0, timeout_seconds=0.05)
    h.robot.alerts = slow
    started = asyncio.get_running_loop().time()
    await h.robot._alert("halted", "y")
    assert asyncio.get_running_loop().time() - started < 0.5
    await slow.drain()
    assert h.robot.bot_state is BotState.RUNNING


async def test_every_heartbeat_and_state_event_carries_risk_and_config_version() -> None:
    h = await Harness().start()
    await h.robot._publish_state("probe")
    await h.robot.housekeeping()
    await h.robot._emit("heartbeat", h.robot.status_payload(h.clock.now()))

    for kind in ("state", "heartbeat"):
        events = h.bus.of(kind)
        assert events, kind
        for payload in events:
            assert isinstance(payload["risk"], dict) and payload["risk"]
            assert "config_version" in payload and payload["state"] == "RUNNING"
