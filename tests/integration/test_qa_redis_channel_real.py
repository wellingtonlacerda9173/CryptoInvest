"""QA 2a rodada: canal de comandos contra o Redis REAL (prefixo unico por teste, limpo ao final)."""

import json
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from redis import asyncio as aioredis

from engine.runner.command_channel import RedisCommandSource, sign_command
from engine.runner.paper_control import Command, CommandName, CommandQueue
from engine.runner.paper_events import RedisKeys
from engine.runner.paper_robot import BotState
from engine.runner.signing import open_ack, open_event, sign_ack, wrap_event
from tests.unit.runner.fakes import Harness
from tests.unit.runner.test_paper_signed_commands import KEY, AuditRecorder

pytestmark = pytest.mark.integration
URL = "redis://localhost:6379/0"


class Rig:
    def __init__(self, h: Harness, raw: Any, keys: RedisKeys, tmp: Path) -> None:
        self.h, self.raw, self.keys, self.tmp = h, raw, keys, tmp
        self.audit = AuditRecorder()
        self.source = self.new_source()
        h.robot.commands = CommandQueue(tmp)
        h.robot.audit_sink = self.audit
        self.n = 0

    def new_source(self) -> RedisCommandSource:
        client = aioredis.from_url(URL, socket_connect_timeout=3, socket_timeout=2, decode_responses=False)
        source = RedisCommandSource(client, KEY, keys=self.keys)
        self.h.robot.signed_commands = source
        self.source = source
        return source

    def envelope(self, name: CommandName, command_id: str | None = None, **fields: Any) -> tuple[str, str]:
        self.n += 1
        command_id = command_id or f"qa{self.n:010d}x"
        now = self.h.clock.now()
        command = Command(name=name, operator="ana", reason="teste", issued_at=now, **fields)
        return command_id, sign_command(KEY, command, command_id=command_id, signed_at=now)

    async def push(self, *items: str | bytes) -> None:
        await self.raw.rpush(self.keys.commands, *items)

    async def ack(self, command_id: str) -> dict[str, Any] | None:
        value = await self.raw.get(self.keys.ack(command_id))
        return None if value is None else open_ack(KEY, command_id, value.decode())


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    raw = aioredis.from_url(URL, decode_responses=False)
    try:
        await raw.ping()
    except Exception:
        pytest.skip("Redis indisponivel")
    keys = RedisKeys(f"qa2test:{uuid.uuid4().hex[:8]}")
    instance = Rig(await Harness().start(), raw, keys, tmp_path)
    yield instance
    async for key in raw.scan_iter(match=f"{keys.prefix}:*"):
        await raw.delete(key)
    await instance.source.close()
    await raw.aclose()


async def test_non_utf8_poison_does_not_block_a_legit_emergency_stop(rig: Rig) -> None:
    command_id, good = rig.envelope(CommandName.EMERGENCY_STOP)
    await rig.push(b"\xff\xfe\xfa", good.encode())

    for _ in range(3):
        await rig.h.robot.housekeeping()

    assert rig.h.robot.bot_state is BotState.PAUSED
    assert (await rig.ack(command_id) or {}).get("result") == "applied"


async def test_ascii_and_unicode_poison_in_the_same_batch_never_costs_the_legit_command(rig: Rig) -> None:
    command_id, good = rig.envelope(CommandName.EMERGENCY_STOP)
    poison: list[str | bytes] = [
        "x" * 20_000,
        "[" * 3000,
        '{"payload":{"id":"' + "9" * 6000 + '"},"sig":"a"}',
        '{"payload":{"v":NaN},"sig":"é"}',
        '{"payload":{"id":"abcdefgh1","id":"abcdefgh2"},"sig":"a","sig":"b"}',
        "‮\u0000".encode(),
        json.dumps({"payload": "x", "sig": 1}),
        "",
    ]
    await rig.push(*poison, good.encode())

    await rig.h.robot.housekeeping()

    assert rig.h.robot.bot_state is BotState.PAUSED
    assert (await rig.ack(command_id) or {}).get("result") == "applied"
    assert await rig.raw.llen(rig.keys.processing) == 0


async def test_crash_between_lmove_and_local_write_then_restart_executes_exactly_once(rig: Rig) -> None:
    robot = rig.h.robot
    command_id, good = rig.envelope(CommandName.EMERGENCY_STOP)
    await rig.push(good.encode())
    real_send = robot.commands.send  # type: ignore[union-attr]

    def explode(*args: Any, **kwargs: Any) -> Any:
        raise OSError("processo morreu")

    robot.commands.send = explode  # type: ignore[method-assign,union-attr]
    await robot.housekeeping()
    assert await rig.raw.llen(rig.keys.processing) == 1
    assert not await rig.raw.exists(rig.keys.nonce(command_id)), "nonce nao pode ser reivindicado sem gravar"

    robot.commands.send = real_send  # type: ignore[method-assign,union-attr]
    rig.new_source()  # runner reiniciou: guard em memoria vazio
    await robot.housekeeping()
    await robot.housekeeping()

    assert robot.bot_state is BotState.PAUSED
    assert (await rig.ack(command_id) or {}).get("result") == "applied"
    assert await rig.raw.llen(rig.keys.processing) == 0
    executed = [r for r in rig.audit.rows if r[1] == "paper_command_discarded"]
    assert executed == [], "reentrega legitima nao pode virar descarte"


async def test_crash_after_nonce_claim_but_before_lrem_does_not_redo_or_lose(rig: Rig) -> None:
    robot = rig.h.robot
    command_id, good = rig.envelope(CommandName.PAUSE)
    await rig.push(good.encode())
    real_release = rig.source.release

    async def die(outcome: Any) -> None:
        raise RuntimeError("morreu antes do LREM")

    rig.source.release = die  # type: ignore[method-assign]
    await robot.housekeeping()  # grava, reivindica o nonce e "morre" antes do LREM
    assert await rig.raw.exists(rig.keys.nonce(command_id))
    rig.source.release = real_release  # type: ignore[method-assign]
    rig.new_source()
    await robot.housekeeping()
    await robot.housekeeping()

    assert await rig.raw.llen(rig.keys.processing) == 0
    assert robot.bot_state is BotState.PAUSED
    assert (await rig.ack(command_id) or {}).get("result") in {"applied", "received"}


async def test_the_same_envelope_pushed_twice_executes_once_and_keeps_the_real_result(rig: Rig) -> None:
    command_id, good = rig.envelope(CommandName.PAUSE)
    await rig.push(good.encode(), good.encode())
    await rig.h.robot.housekeeping()
    await rig.h.robot.housekeeping()
    result = await rig.ack(command_id)

    assert result is not None and result["result"] == "applied"
    assert len(list(rig.tmp.glob("*~*.json"))) == 0, "o comando foi consumido uma unica vez"
    assert len(list((rig.tmp / "processed").glob("*.json"))) == 1


async def test_ack_signed_by_someone_without_the_key_or_for_another_id_is_not_trusted(rig: Rig) -> None:
    forged = sign_ack(b"z" * 40, "abcdefgh12", {"result": "applied", "detail": "", "at": "x"})
    other = sign_ack(KEY, "outroidxx99", {"result": "applied", "detail": "", "at": "x"})

    assert open_ack(KEY, "abcdefgh12", json.dumps(forged)) is None
    assert open_ack(KEY, "abcdefgh12", json.dumps(other)) is None


async def test_an_event_signature_is_not_valid_as_an_ack_and_vice_versa(rig: Rig) -> None:
    ev = wrap_event(KEY, "heartbeat", {"result": "applied", "detail": "", "at": "x"})
    as_ack = {"result": "applied", "detail": "", "at": "x", "sig": json.loads(ev)["sig"]}
    ack = sign_ack(KEY, "heartbeat", {"payload": {"result": "applied"}})
    forged_event = json.dumps({"kind": "heartbeat", "payload": {"result": "applied"}, "sig": ack["sig"]})

    assert open_ack(KEY, "heartbeat", json.dumps(as_ack)) is None
    assert open_event(KEY, "heartbeat", forged_event) is None


async def test_flood_of_junk_above_the_cap_does_not_starve_a_following_emergency_stop(rig: Rig) -> None:
    command_id, good = rig.envelope(CommandName.EMERGENCY_STOP)
    await rig.push(*[f"lixo-{i}" for i in range(1500)])
    await rig.push(good.encode())
    for _ in range(40):
        await rig.h.robot.housekeeping()
        if rig.h.robot.bot_state is BotState.PAUSED:
            break

    assert rig.h.robot.bot_state is BotState.PAUSED, "emergency-stop atras de 1500 lixos nunca chegou"
    assert len([r for r in rig.audit.rows if r[1] == "paper_command_discarded"]) <= 5 * 40
    assert command_id


async def test_stale_emergency_stop_discard_is_acked_so_the_api_can_resend(rig: Rig) -> None:
    now = rig.h.clock.now()
    command = Command(name=CommandName.EMERGENCY_STOP, operator="ana", reason="t", issued_at=now)
    old = sign_command(KEY, command, command_id="stale0000001", signed_at=now - timedelta(minutes=5))
    await rig.push(old.encode())

    await rig.h.robot.housekeeping()

    ack = await rig.ack("stale0000001")
    assert ack is not None and (ack["result"], ack["detail"]) == ("discarded", "stale")


async def test_a_forged_envelope_reusing_a_real_id_cannot_overwrite_the_real_ack(rig: Rig) -> None:
    command_id, good = rig.envelope(CommandName.PAUSE)
    await rig.push(good.encode())
    await rig.h.robot.housekeeping()
    assert (await rig.ack(command_id) or {}).get("result") == "applied"

    forged = json.loads(good)
    forged["payload"]["reason"] = "outro motivo"
    await rig.push(json.dumps(forged))
    await rig.h.robot.housekeeping()

    assert (await rig.ack(command_id) or {}).get("result") == "applied"


async def test_crash_after_claim_before_execution_then_restart_still_executes_the_command(rig: Rig) -> None:
    robot = rig.h.robot
    command_id, good = rig.envelope(CommandName.EMERGENCY_STOP)
    await rig.push(good.encode())
    real_release = rig.source.release

    async def die(outcome: Any) -> None:
        raise RuntimeError("morreu antes do LREM")

    rig.source.release = die  # type: ignore[method-assign]
    await robot._pull_signed(robot.clock.now())  # grava e reivindica o nonce; NAO executa
    rig.source.release = real_release  # type: ignore[method-assign]
    assert await rig.raw.exists(rig.keys.nonce(command_id))
    assert len(list(rig.tmp.glob("*~*.json"))) == 1 and robot.bot_state is BotState.RUNNING

    rig.new_source()  # reinicio: o envelope reaparece, o nonce ja esta reivindicado
    await robot.housekeeping()

    assert robot.bot_state is BotState.PAUSED, "o comando gravado foi apagado como se fosse repeticao"
    assert (await rig.ack(command_id) or {}).get("result") == "applied"


async def test_invalid_utf8_poison_is_discarded_as_malformed_and_removed_with_the_original_bytes(
    rig: Rig,
) -> None:
    poison = [b"\xff\xfe\xfa", b"\x80abc", b'{"payload":{},"sig":"\xc3\x28"}']
    first_id, first = rig.envelope(CommandName.PAUSE)
    await rig.push(poison[0], first.encode(), poison[1], poison[2])
    second_id, second = rig.envelope(CommandName.EMERGENCY_STOP)
    await rig.push(second.encode())

    for _ in range(4):
        await rig.h.robot.housekeeping()

    assert rig.h.robot.bot_state is BotState.PAUSED
    assert await rig.raw.llen(rig.keys.processing) == 0 and await rig.raw.llen(rig.keys.commands) == 0
    assert (await rig.ack(first_id) or {}).get("result") == "applied"
    assert (await rig.ack(second_id) or {}).get("result") == "applied"
    codes = [r[3]["code"] for r in rig.audit.rows if r[1] == "paper_command_discarded"]
    assert codes and set(codes) == {"malformed"}


async def test_poison_between_many_legit_commands_never_blocks_any_of_them(rig: Rig) -> None:
    ids = []
    for index in range(6):
        command_id, good = rig.envelope(CommandName.PAUSE if index % 2 else CommandName.RESUME)
        ids.append(command_id)
        await rig.push(b"\xff" * (index + 1), good.encode())

    for _ in range(6):
        await rig.h.robot.housekeeping()

    assert await rig.raw.llen(rig.keys.processing) == 0
    for command_id in ids:
        assert await rig.ack(command_id) is not None


async def test_non_utf8_values_in_ack_and_event_keys_never_break_the_api_link(rig: Rig) -> None:
    from api.runner_link import RedisRunnerLink
    from engine.infrastructure.config.settings import load_settings
    from tests.integration.api.runner_support import redis_url_of

    link = RedisRunnerLink(redis_url_of(load_settings()), rig.keys, signing_key=KEY)
    try:
        await rig.raw.set(rig.keys.latest("heartbeat"), b"\xff\xfe")
        await rig.raw.set(rig.keys.latest("state"), wrap_event(KEY, "state", {"x": 1}).encode())
        await rig.raw.set(rig.keys.ack("poison00001"), b"\xff\x00\xfe")

        probe = await link.probe()
        ack = await link.ack("poison00001")
    finally:
        await link.close()

    assert probe.reachable and set(probe.latest) == {"state"}
    assert ack is None
    assert link.rejections.total >= 2


async def test_the_source_refuses_a_client_that_decodes_responses() -> None:
    client = aioredis.from_url(URL, decode_responses=True)
    try:
        with pytest.raises(ValueError, match="decode_responses"):
            RedisCommandSource(client, KEY)
    finally:
        await client.aclose()
