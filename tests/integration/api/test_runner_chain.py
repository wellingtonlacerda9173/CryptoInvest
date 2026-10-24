"""API <-> runner com Postgres e Redis REAIS: o comando assinado chega ao robo e e aplicado; a fila duravel
sobrevive a Redis fora; envelopes falsos, velhos ou repetidos sao descartados e auditados."""

import asyncio
import contextlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from api.runner_link import RedisEventSource, RedisRunnerLink
from api.services.commands import CommandService
from api.services.realtime import RealtimeHub
from engine.runner.command_channel import RedisCommandSource, sign_command
from engine.runner.paper_control import Command, CommandName, CommandQueue
from engine.runner.paper_events import RedisEventBus, RedisKeys
from engine.runner.paper_robot import BotState
from tests.integration.api.conftest import Harness, Session
from tests.integration.api.runner_support import (
    COMMAND_KEY,
    build_harness,
    module_db_url,
    module_sessions,
    redis_link,
    redis_url_of,
)
from tests.unit.runner.fakes import FakeClock
from tests.unit.runner.fakes import Harness as RobotHarness

pytestmark = pytest.mark.integration


class AuditRecorder:
    def __init__(self) -> None:
        self.rows: list[tuple[str, Mapping[str, Any]]] = []

    async def audit(self, actor: str, action: str, entity: str, payload: Mapping[str, Any]) -> None:
        self.rows.append((action, payload))


class Chain:
    """Robo fake (diario em memoria) ligado ao MESMO Redis da API, com o relogio da API."""

    def __init__(
        self,
        api: Harness,
        admin: Session,
        robot: RobotHarness,
        source: RedisCommandSource,
        audit: AuditRecorder,
    ) -> None:
        self.api, self.admin, self.h, self.source, self.audit = api, admin, robot, source, audit

    async def tick(self) -> None:
        self.h.clock.set(self.api.clock())
        await self.h.robot.housekeeping()

    async def announce(self) -> None:
        self.h.clock.set(self.api.clock())
        await self.h.robot._publish_state("start")
        for _ in range(100):
            if (await self.admin.get("/status")).json()["connection"] == "live":
                return
            await asyncio.sleep(0.05)
        raise AssertionError("a API nunca viu o heartbeat do robo")


@pytest.fixture
async def chain(module_sessions: Any, redis_link: RedisRunnerLink, tmp_path: Path) -> Any:
    api = build_harness(module_sessions, redis_link, redis_prefix=redis_link.keys.prefix)
    admin = await api.login(await api.create_user("admin"))
    robot = await RobotHarness(clock=FakeClock(api.clock())).start()
    from engine.infrastructure.config.settings import load_settings

    url = redis_url_of(load_settings())
    keys = redis_link.keys
    bus = RedisEventBus(url, keys=keys)
    source = RedisCommandSource.from_url(url, COMMAND_KEY.encode(), keys=keys)
    audit = AuditRecorder()
    robot.robot.bus = bus
    robot.robot.commands = CommandQueue(tmp_path / "commands")
    robot.robot.signed_commands = source
    robot.robot.audit_sink = audit
    link = Chain(api, admin, robot, source, audit)
    yield link
    await bus.close()
    await source.close()
    await admin.aclose()


async def applied(chain: Chain, command_id: str) -> dict[str, Any]:
    for _ in range(40):
        await chain.tick()
        await CommandService(chain.api.container).reconcile()
        body = (await chain.admin.get(f"/control/commands/{command_id}")).json()
        if body["state"] in {"applied", "failed", "discarded", "expired"}:
            return body  # type: ignore[no-any-return]
        await asyncio.sleep(0.05)
    raise AssertionError(body)


async def test_a_signed_pause_goes_through_redis_and_the_robot_applies_it(chain: Chain) -> None:
    await chain.announce()
    response = await chain.admin.send(
        "POST", "/control/pause", json={"reason": "manutencao programada", "operator_name": "Ana"}
    )
    body = response.json()
    assert response.status_code == 202 and body["state"] == "delivered" and body["channel"] == "redis"

    final = await applied(chain, body["id"])
    assert final["state"] == "applied"
    assert chain.h.robot.bot_state is BotState.PAUSED
    state = chain.h.robot.state
    assert state.pause_reason == "manutencao programada"
    operator = f"Ana ({chain.admin.username})"
    assert chain.h.robot._pauses["manutencao programada"].operator == operator
    assert [(e.action.value, e.operator) for e in chain.h.journal.controls][-1] == ("pause", operator)
    status = (await chain.admin.get("/status")).json()
    assert status["pending_commands"] == 0


async def test_the_runner_never_relaxes_a_limit_even_if_the_api_asks(chain: Chain) -> None:
    await chain.announce()
    reduced = (
        await chain.admin.send(
            "PUT",
            "/settings/risk",
            json={"value": {"risk_per_trade": "0.003"}, "reason": "mais prudente", "expected_version": 0},
        )
    ).json()
    assert reduced["command"]["state"] == "delivered"
    assert (await applied(chain, reduced["command"]["id"]))["state"] == "applied"
    assert str(chain.h.robot.risk.risk_per_trade) == "0.003"
    assert [r[0] for r in chain.audit.rows] == ["config_applied"]

    forged = Command(
        name=CommandName.APPLY_CONFIG, operator="api", reason="relaxar", issued_at=chain.api.clock(),
        params={"risk": {"risk_per_trade": "0.009"}},
    )  # fmt: skip
    await chain.api.container.link.push(  # type: ignore[union-attr]
        sign_command(COMMAND_KEY.encode(), forged, command_id="relax0000000001", signed_at=chain.api.clock())
    )
    await chain.tick()
    assert str(chain.h.robot.risk.risk_per_trade) == "0.003"
    assert chain.audit.rows[-1][0] == "config_apply_refused"


async def test_forged_replayed_and_stale_envelopes_are_discarded_and_audited(chain: Chain) -> None:
    await chain.announce()
    link = chain.api.container.link
    assert link is not None
    pause = Command(name=CommandName.PAUSE, operator="mallory", reason="forjado", issued_at=chain.api.clock())
    await link.push(sign_command(b"x" * 48, pause, command_id="forged00000001", signed_at=chain.api.clock()))
    stale = sign_command(
        COMMAND_KEY.encode(),
        pause,
        command_id="stale000000001",
        signed_at=chain.api.clock().replace(year=2020),
    )
    await link.push(stale)
    genuine = sign_command(
        COMMAND_KEY.encode(), pause, command_id="genuine0000001", signed_at=chain.api.clock()
    )
    await link.push(genuine)
    await link.push(genuine)
    await chain.tick()
    codes = [row[1]["code"] for row in chain.audit.rows if row[0] == "paper_command_discarded"]
    assert codes == ["bad_signature", "stale", "replay"]
    assert chain.h.robot.bot_state is BotState.PAUSED
    assert chain.h.robot.state.pause_reason == "forjado"
    raw = json.dumps([r[1] for r in chain.audit.rows])
    assert COMMAND_KEY not in raw and "mallory" not in raw


async def test_commands_queue_durably_while_redis_is_down_and_flow_when_it_returns(
    module_sessions: Any, redis_link: RedisRunnerLink, tmp_path: Path
) -> None:
    from engine.infrastructure.config.settings import load_settings

    down = RedisRunnerLink("redis://127.0.0.1:6399/0", redis_link.keys)
    api = build_harness(module_sessions, down, redis_prefix=redis_link.keys.prefix)
    admin = await api.login(await api.create_user("admin"))

    pause = (await admin.send("POST", "/control/pause", json={"reason": "enquanto o redis esta fora"})).json()
    emergency = (await admin.send("POST", "/control/emergency-stop", json={})).json()
    assert pause["state"] == emergency["state"] == "pending_delivery"
    assert (await admin.get("/status")).json()["connection"] == "unknown"

    api.container.link = redis_link
    robot = await RobotHarness(clock=FakeClock(api.clock())).start()
    keys = redis_link.keys
    bus = RedisEventBus(redis_url_of(load_settings()), keys=keys)
    source = RedisCommandSource.from_url(redis_url_of(load_settings()), COMMAND_KEY.encode(), keys=keys)
    robot.robot.bus, robot.robot.commands, robot.robot.signed_commands = (
        bus,
        CommandQueue(tmp_path / "c"),
        source,
    )
    chain = Chain(api, admin, robot, source, AuditRecorder())
    await chain.announce()
    delivered = await CommandService(api.container).dispatch_pending()
    assert delivered >= 2
    results = {c: (await applied(chain, c))["state"] for c in (pause["id"], emergency["id"])}
    assert results[emergency["id"]] == "applied"
    assert robot.robot.bot_state is BotState.PAUSED
    assert results[pause["id"]] in {"applied", "expired"}
    await bus.close()
    await source.close()
    await down.close()
    await admin.aclose()


async def test_real_redis_pubsub_feeds_the_realtime_hub(redis_link: RedisRunnerLink) -> None:
    from engine.infrastructure.config.settings import load_settings

    url, keys = redis_url_of(load_settings()), redis_link.keys
    hub = RealtimeHub(RedisEventSource(url, keys), clock=lambda: datetime.now(UTC))
    sub = hub.register("u", 3)
    assert sub is not None
    sub.channels = {"decisions", "alerts", "status"}
    task = asyncio.create_task(hub.run())
    await asyncio.sleep(0.5)
    bus = RedisEventBus(url, keys=keys)
    await bus.publish("decisions", {"run_id": "r", "code": "no_signal"})
    await bus.publish("risk", {"action": "daily_loss_pause"})
    await bus.publish("state", {"state": "RUNNING"})
    messages: list[dict[str, Any]] = []
    for _ in range(100):
        while not sub.queue.empty():
            messages.append(json.loads(sub.queue.get_nowait()))
        if len([m for m in messages if m["type"] == "event"]) >= 3:
            break
        await asyncio.sleep(0.05)
    assert [m["channel"] for m in messages if m["type"] == "event"] == ["decisions", "alerts", "status"]
    assert hub.runner_connected
    await bus.close()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
