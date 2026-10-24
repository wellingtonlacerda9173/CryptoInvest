"""QA (3a rodada): sondas do canal de comandos com Redis REAL e do A2 de ponta a ponta.

`xfail(strict=True, reason="BUG: ...")` comprova defeito aberto; ao corrigir, o xfail estrito falha e obriga
a remover a marca. Redis de desenvolvimento com prefixo exclusivo por teste (limpo no final).
"""

import secrets
from collections.abc import AsyncIterator, Mapping
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from redis import asyncio as aioredis

from engine.infrastructure.config.settings import load_settings
from engine.runner.command_channel import RedisCommandSource, sign_command
from engine.runner.paper_control import Command, CommandName, CommandQueue
from engine.runner.paper_events import RedisKeys
from engine.runner.paper_robot import BotState
from engine.runner.signing import open_ack
from tests.integration.api.conftest import Harness, Session
from tests.integration.api.runner_support import (
    COMMAND_KEY,
    fake_link,
    module_db_url,
    module_sessions,
    redis_url_of,
    rh,
    rh_admin,
    status_payload,
)
from tests.redis_fakes import FakeLink
from tests.unit.runner.fakes import FakeClock
from tests.unit.runner.fakes import Harness as RobotHarness

pytestmark = pytest.mark.integration
KEY = COMMAND_KEY.encode()


class Audit:
    def __init__(self) -> None:
        self.rows: list[tuple[str, Mapping[str, Any]]] = []

    async def audit(self, actor: str, action: str, entity: str, payload: Mapping[str, Any]) -> None:
        self.rows.append((action, payload))


class Rig:
    def __init__(
        self, robot: RobotHarness, source: RedisCommandSource, keys: RedisKeys, raw: Any, audit: Audit
    ):
        self.h, self.source, self.keys, self.raw, self.audit = robot, source, keys, raw, audit

    def envelope(self, name: CommandName, command_id: str, **kw: Any) -> str:
        now = self.h.clock.now()
        command = Command(name=name, operator="t", reason="motivo", issued_at=now, **kw)
        return sign_command(KEY, command, command_id=command_id, signed_at=now)

    async def push(self, text: str | bytes) -> None:
        await self.raw.rpush(self.keys.commands, text)

    async def tick(self) -> None:
        await self.h.robot.housekeeping()


@pytest_asyncio.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    url = redis_url_of(load_settings())
    keys = RedisKeys(f"cryptoinvest:test:qa3{secrets.token_hex(3)}")
    raw = aioredis.from_url(url, decode_responses=False)
    try:
        await raw.ping()
    except Exception:
        pytest.skip("Redis indisponivel")
    robot = await RobotHarness(
        clock=FakeClock(__import__("datetime").datetime.now(__import__("datetime").UTC))
    ).start()
    source = RedisCommandSource.from_url(url, KEY, keys=keys)
    audit = Audit()
    robot.robot.commands = CommandQueue(tmp_path / "commands")
    robot.robot.signed_commands = source
    robot.robot.audit_sink = audit
    yield Rig(robot, source, keys, raw, audit)
    for pattern in (f"{keys.prefix}:*",):
        async for k in raw.scan_iter(match=pattern):
            await raw.delete(k)
    await source.close()
    await raw.aclose()


async def test_invalid_utf8_poison_does_not_block_a_legitimate_emergency_stop(rig: Rig) -> None:
    await rig.push(b"\xff\xfe\x80 nao e utf-8")
    await rig.push(rig.envelope(CommandName.EMERGENCY_STOP, "emerg00000001"))
    for _ in range(3):
        await rig.tick()
    assert rig.h.robot.bot_state is BotState.PAUSED
    assert await rig.raw.llen(rig.keys.processing) == 0


async def test_crash_after_lmove_before_commit_redelivers_and_executes_once(rig: Rig, tmp_path: Path) -> None:
    await rig.push(rig.envelope(CommandName.PAUSE, "pause000000001"))
    # "crash": o runner move o envelope para processing e morre antes de gravar/ack
    await rig.source.poll(rig.h.clock.now())
    assert await rig.raw.llen(rig.keys.processing) == 1
    assert await rig.raw.llen(rig.keys.commands) == 0
    # reinicio: nova fonte (guarda em memoria vazio)
    url = redis_url_of(load_settings())
    fresh = RedisCommandSource.from_url(url, KEY, keys=rig.keys)
    rig.h.robot.signed_commands = fresh
    for _ in range(3):
        await rig.tick()
    assert rig.h.robot.bot_state is BotState.PAUSED
    assert await rig.raw.llen(rig.keys.processing) == 0
    pauses = [c for c in rig.h.journal.controls if c.action.value == "pause"]
    assert len(pauses) == 1
    await fresh.close()


async def test_crash_between_nonce_claim_and_release_does_not_lose_or_duplicate(rig: Rig) -> None:
    envelope = rig.envelope(CommandName.PAUSE, "pause000000002")
    await rig.push(envelope)
    outcomes = await rig.source.poll(rig.h.clock.now())
    outcome = outcomes[0]
    # gravou localmente e reivindicou o nonce, mas morreu antes do LREM
    rig.h.robot.commands.send(outcome.command, outcome.command_id)  # type: ignore[arg-type]
    assert await rig.source._claim(outcome.command_id)  # type: ignore[arg-type]
    url = redis_url_of(load_settings())
    fresh = RedisCommandSource.from_url(url, KEY, keys=rig.keys)
    rig.h.robot.signed_commands = fresh
    for _ in range(3):
        await rig.tick()
    assert rig.h.robot.bot_state is BotState.PAUSED
    assert await rig.raw.llen(rig.keys.processing) == 0
    assert len([c for c in rig.h.journal.controls if c.action.value == "pause"]) == 1
    await fresh.close()


async def test_nonce_claimed_without_local_write_in_a_new_run_is_a_replay_not_a_silent_loss(rig: Rig) -> None:
    """Runner novo (fila local nova) apos reivindicar o nonce no run anterior: o comando e descartado e
    AUDITADO como replay (nunca em silencio) e o envelope sai de processing."""
    envelope = rig.envelope(CommandName.PAUSE, "pause000000003")
    await rig.push(envelope)
    outcome = (await rig.source.poll(rig.h.clock.now()))[0]
    assert await rig.source._claim(outcome.command_id)  # type: ignore[arg-type]
    for _ in range(2):
        await rig.tick()
    assert rig.h.robot.bot_state is not BotState.PAUSED
    assert await rig.raw.llen(rig.keys.processing) == 0
    assert [r[1]["code"] for r in rig.audit.rows if r[0] == "paper_command_discarded"] == ["replay"]


async def test_duplicate_envelopes_in_the_same_batch_and_emergency_duplicate_are_noops(rig: Rig) -> None:
    first = rig.envelope(CommandName.EMERGENCY_STOP, "emerg00000002")
    second = rig.envelope(CommandName.EMERGENCY_STOP, "emerg00000002-2")
    for e in (first, first, second):
        await rig.push(e)
    for _ in range(3):
        await rig.tick()
    stops = [c for c in rig.h.journal.controls if c.action.value == "pause"]
    assert len(stops) == 1
    stored = await rig.raw.get(rig.keys.ack("emerg00000002"))
    ack = open_ack(KEY, "emerg00000002", stored.decode())
    assert ack is not None and ack["result"] == "applied"


@pytest.mark.parametrize(
    "poison",
    [
        b'{"payload":{"id":"x"},"sig":"' + b"a" * 9000 + b'"}',
        b"[" * 7000,
        b'{"payload":' + b"[" * 9 + b"]" * 9 + b',"sig":"x"}',
        b'{"payload":{"v":1,"id":"abcdefgh","ts":1e99999,"name":"pause"},"sig":"\xc3\xa9"}',
        b'{"payload":{},"sig":"x","payload":{"id":"abcdefgh"}}',
        b"1" * 5000,
        b'{"payload":{"n":' + b"9" * 5000 + b'},"sig":"x"}',
        b'{"payload":{"a":"\\ud800"},"sig":"\\ud800"}',
        b"\x00\x01\x02",
        b"null",
        b'"\\u0000"',
    ],
)
async def test_each_poison_format_is_discarded_and_the_next_legit_command_runs(
    rig: Rig, poison: bytes
) -> None:
    await rig.push(poison)
    await rig.push(rig.envelope(CommandName.PAUSE, "pause" + secrets.token_hex(4)))
    for _ in range(3):
        await rig.tick()
    assert rig.h.robot.bot_state is BotState.PAUSED
    assert await rig.raw.llen(rig.keys.processing) == 0


async def test_flood_over_the_cap_still_reaches_a_command_pushed_after_it(rig: Rig) -> None:
    async with rig.raw.pipeline() as pipe:
        for i in range(1500):
            pipe.rpush(rig.keys.commands, f"junk-{i}")
        await pipe.execute()
    await rig.push(rig.envelope(CommandName.EMERGENCY_STOP, "emerg00000003"))
    ticks = 0
    while rig.h.robot.bot_state is not BotState.PAUSED and ticks < 20:
        await rig.tick()
        ticks += 1
    assert rig.h.robot.bot_state is BotState.PAUSED, f"nao chegou em {ticks} polls"


async def test_ack_signature_is_domain_separated_from_events(rig: Rig) -> None:
    import json

    from engine.runner.signing import sign_body, wrap_event

    ev = json.loads(wrap_event(KEY, "heartbeat", {"result": "applied", "detail": "", "at": "x"}))
    forged = {"result": "applied", "detail": "", "at": "x", "sig": ev["sig"]}
    assert open_ack(KEY, "heartbeat", json.dumps(forged)) is None
    assert open_ack(b"z" * 48, "abc12345", json.dumps(forged)) is None
    body = {"result": "applied", "detail": "", "at": "x"}
    ok = {**body, "sig": sign_body(KEY, "ack", "abc12345", body)}
    assert open_ack(KEY, "abc12345", json.dumps(ok)) == body
    assert open_ack(KEY, "other123", json.dumps(ok)) is None


# ---------------------------------------------------------------------------------------------- A2


async def test_reduction_reaches_the_robot_even_if_the_equity_event_is_the_freshest(
    rh: Harness, rh_admin: Session, fake_link: FakeLink
) -> None:
    now = rh.clock()
    risk = (await rh_admin.get("/settings")).json()["document"]["risk"]
    fake_link.latest["heartbeat"] = status_payload(now=now - timedelta(seconds=3), risk=risk)
    fake_link.latest["equity"] = {
        "run_id": "paper-it-1",
        "at": now.isoformat(),
        "kind": "equity",
        "equity": "1",
    }
    fake_link.signed_now = now
    version = (await rh_admin.get("/settings")).json()["version"]
    body = {
        "value": {"risk_per_trade": "0.002"},
        "reason": "mais prudente",
        "expected_version": 0 if version is None else version["id"],
    }
    response = await rh_admin.send("PUT", "/settings/risk", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["command"] is not None, response.json()["activation_text"]
