"""QA FINAL: canal de comandos e links no Redis REAL, com o consumidor real do runner e relogio normal."""

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from redis import asyncio as aioredis

from api.runner_link import RedisRunnerLink
from engine.runner.command_channel import RedisCommandSource
from engine.runner.paper_control import CommandName, CommandQueue
from engine.runner.paper_events import RedisKeys
from engine.runner.paper_robot import BotState
from engine.runner.signing import sign_ack, wrap_event
from tests.integration.test_qa_redis_channel_real import KEY, URL, Rig
from tests.unit.runner.fakes import FakeClock, Harness

pytestmark = pytest.mark.integration


class RealClock(FakeClock):
    """Relogio normal (parede), nao o congelado dos fakes."""

    def now(self) -> datetime:
        return datetime.now(UTC)


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    raw = aioredis.from_url(URL, decode_responses=False)
    try:
        await raw.ping()
    except Exception:
        pytest.skip("Redis indisponivel")
    keys = RedisKeys(f"qafinal:{uuid.uuid4().hex[:8]}")
    harness = Harness(clock=RealClock())
    instance = Rig(await harness.start(), raw, keys, tmp_path)
    instance.h.robot.commands = CommandQueue(tmp_path)
    yield instance
    async for key in raw.scan_iter(match=f"{keys.prefix}:*"):
        await raw.delete(key)
    await instance.source.close()
    await raw.aclose()


def poison_formats(good: bytes) -> list[bytes]:
    return [
        b"\xff\xfe",
        b"\xc3",  # meio de uma sequencia UTF-8
        b"\xef\xbb\xbf" + good,  # BOM + envelope legitimo
        b"\xef\xbb\xbf",
        b"\xed\xa0\x80",  # surrogate codificado (CESU): UTF-8 invalido
        b'{"payload":{"id":"abcdefgh1","reason":"\\ud800"},"sig":"zz"}',  # surrogate escapado em JSON valido
        b"\x00" * 50,
        b'{"payload":{"x":"\xf0\x28\x8c\xbc"},"sig":"a"}',
        b"\x80" * 9000,  # grande e invalido
        good[: len(good) // 2],  # truncado
        b"\xff" + good,  # legitimo precedido de byte invalido
        good + b"\xff",  # legitimo seguido de byte invalido
        b"[" * 100_000,  # profundidade absurda
        b'{"payload":' * 500 + b"1" + b"}" * 500,
        b"\xfe\xff\x00{\x00}",  # BOM UTF-16
        "😀".encode("utf-8", "surrogatepass") * 3,
        b'"' * 10,
        b"null",
        b"123456789012345678901234567890",
    ]


async def test_nineteen_poison_formats_mixed_with_legit_batches_then_emergency_stop(rig: Rig) -> None:
    ids: list[str] = []
    for index in range(6):
        command_id, good = rig.envelope(CommandName.PAUSE if index % 2 else CommandName.RESUME)
        ids.append(command_id)
        await rig.push(*poison_formats(good.encode())[index::3], good.encode())
    stop_id, stop = rig.envelope(CommandName.EMERGENCY_STOP)
    await rig.push(*poison_formats(stop.encode()), stop.encode())

    for _ in range(60):
        await rig.h.robot.housekeeping()
        if (
            rig.h.robot.bot_state is BotState.PAUSED
            and await rig.raw.llen(rig.keys.processing) + await rig.raw.llen(rig.keys.commands) == 0
        ):
            break

    assert rig.h.robot.bot_state is BotState.PAUSED
    assert await rig.raw.llen(rig.keys.processing) == 0 and await rig.raw.llen(rig.keys.commands) == 0
    assert (await rig.ack(stop_id) or {}).get("result") == "applied"
    for command_id in ids:
        assert await rig.ack(command_id) is not None, command_id


async def test_1500_junk_of_every_kind_then_emergency_stop_with_real_clock(rig: Rig) -> None:
    _, sample = rig.envelope(CommandName.PAUSE)
    kinds = poison_formats(sample.encode())
    await rig.push(*[kinds[i % len(kinds)] + str(i).encode() for i in range(1500)])
    stop_id, stop = rig.envelope(CommandName.EMERGENCY_STOP)
    await rig.push(stop.encode())

    cycles = 0
    while rig.h.robot.bot_state is not BotState.PAUSED and cycles < 40:
        await rig.h.robot.housekeeping()
        cycles += 1

    assert rig.h.robot.bot_state is BotState.PAUSED, f"nao chegou em {cycles} ciclos"
    assert (await rig.ack(stop_id) or {}).get("result") == "applied"


async def test_forged_envelope_with_real_id_before_and_after_never_overwrites_nor_precreates_the_ack(
    rig: Rig,
) -> None:
    command_id, good = rig.envelope(CommandName.PAUSE)
    forged = json.loads(good)
    forged["payload"]["name"] = "resume"
    # forjado ANTES: nao pode criar ack 'discarded' que mascare o comando verdadeiro
    await rig.push(json.dumps(forged))
    await rig.h.robot.housekeeping()
    assert await rig.ack(command_id) is None

    await rig.push(good.encode())
    await rig.h.robot.housekeeping()
    await rig.h.robot.housekeeping()
    assert (await rig.ack(command_id) or {}).get("result") == "applied"

    await rig.push(json.dumps(forged), good.encode(), b"\xff" + command_id.encode())
    await rig.h.robot.housekeeping()
    assert (await rig.ack(command_id) or {}).get("result") == "applied"


async def test_signed_replay_of_a_real_envelope_does_not_downgrade_a_terminal_ack(rig: Rig) -> None:
    command_id, good = rig.envelope(CommandName.PAUSE)
    await rig.push(good.encode())
    await rig.h.robot.housekeeping()
    for _ in range(5):
        await rig.push(good.encode())
        await rig.h.robot.housekeeping()
    assert (await rig.ack(command_id) or {}).get("result") == "applied"
    assert await rig.raw.llen(rig.keys.processing) == 0


async def test_unauthenticated_junk_never_creates_acks(rig: Rig) -> None:
    await rig.push(b"\xff\xfe", b'{"payload":{"id":"abcdefgh99","v":1},"sig":"deadbeef"}')
    await rig.h.robot.housekeeping()
    assert await rig.ack("abcdefgh99") is None
    assert [k async for k in rig.raw.scan_iter(match=f"{rig.keys.prefix}:cmd:ack:*")] == []


async def test_poll_failures_counts_and_alerts_on_the_5th_blind_cycle_then_recovers(rig: Rig) -> None:
    robot = rig.h.robot
    alerts: list[str] = []
    real_alert = robot._alert

    async def spy(kind: str, text: str, *a: Any, **k: Any) -> Any:
        alerts.append(kind)
        return await real_alert(kind, text, *a, **k)

    robot._alert = spy  # type: ignore[method-assign]
    broken = aioredis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2, decode_responses=False)
    source = RedisCommandSource(broken, KEY, keys=rig.keys)
    robot.signed_commands = source
    for _ in range(7):
        await robot.housekeeping()
    assert source.poll_failures >= 5
    assert alerts.count("command_channel_blind") == 1
    await source.close()

    rig.new_source()
    _command_id, stop = rig.envelope(CommandName.EMERGENCY_STOP)
    await rig.push(stop.encode())
    await robot.housekeeping()
    assert robot.bot_state is BotState.PAUSED
    assert rig.source.poll_failures == 0


async def test_api_link_survives_poison_in_every_key_and_withdraw_ignores_deep_nesting(rig: Rig) -> None:
    link = RedisRunnerLink(URL.replace("localhost", "127.0.0.1"), rig.keys, signing_key=KEY)
    try:
        for kind in ("heartbeat", "state", "equity"):
            await rig.raw.set(rig.keys.latest(kind), b"\xff\xfe\x00")
        await rig.raw.set(rig.keys.ack("abcdefgh01"), b"\xed\xa0\x80")
        await rig.raw.set(
            rig.keys.ack("abcdefgh02"), sign_ack(KEY, "abcdefgh02", {"result": "applied"}) and b"[" * 50000
        )
        probe = await link.probe()
        assert probe.reachable and probe.latest == {}
        assert await link.ack("abcdefgh01") is None and await link.ack("abcdefgh02") is None
        victim = "abcdefgh77"
        await rig.raw.rpush(rig.keys.commands, b"\xff" + victim.encode(), (victim.encode() + b"[" * 20000))
        assert await link.withdraw(victim) == 0
    finally:
        await link.close()


async def test_event_stream_ignores_poison_and_keeps_delivering(rig: Rig) -> None:
    link = RedisRunnerLink(URL.replace("localhost", "127.0.0.1"), rig.keys, signing_key=KEY)
    source = link.events()
    stream = source.stream()
    import asyncio

    async def first() -> tuple[str, dict[str, Any]]:
        async for item in stream:
            return item
        raise AssertionError

    task = asyncio.create_task(first())
    await asyncio.sleep(0.5)
    ch = rig.keys.channel("equity")
    for bad in (b"\xff\xfe", b"\xed\xa0\x80", b"[" * 200000, b"{" * 5000, b"\x00"):
        await rig.raw.publish(ch, bad)
    await rig.raw.publish(ch, wrap_event(KEY, "equity", {"equity": "1"}).encode())
    kind, payload = await asyncio.wait_for(task, 5)
    await stream.aclose()
    await link.close()
    assert kind == "equity" and payload == {"equity": "1"}


async def test_withdraw_survives_deeply_nested_json_that_contains_the_victim_id(rig: Rig) -> None:
    link = RedisRunnerLink(URL.replace("localhost", "127.0.0.1"), rig.keys, signing_key=KEY)
    victim = "abcdefgh77"
    try:
        await rig.raw.rpush(rig.keys.commands, ("[" * 20000 + f'"{victim}"' + "]" * 20000).encode())
        assert await link.withdraw(victim) == 0
    finally:
        await link.close()


async def test_withdraw_removes_only_the_victim_envelope_and_keeps_poison_and_neighbours(rig: Rig) -> None:
    link = RedisRunnerLink(URL.replace("localhost", "127.0.0.1"), rig.keys, signing_key=KEY)
    try:
        keep_id, keep = rig.envelope(CommandName.PAUSE)
        victim_id, victim = rig.envelope(CommandName.EMERGENCY_STOP)
        await rig.push(b"\xff\xfe", keep.encode(), victim.encode(), b"\xed\xa0\x80" + victim_id.encode())

        assert await link.withdraw(victim_id) == 1
        left = await rig.raw.lrange(rig.keys.commands, 0, -1)
        assert keep.encode() in left and victim.encode() not in left and len(left) == 3
        assert await link.withdraw(victim_id) == 0
        assert keep_id
    finally:
        await link.close()


async def test_restart_with_poison_and_legit_items_already_stranded_in_processing_still_applies_the_stop(
    rig: Rig,
) -> None:
    _, pause = rig.envelope(CommandName.PAUSE)
    stop_id, stop = rig.envelope(CommandName.EMERGENCY_STOP)
    # estado deixado por um runner que morreu logo depois do LMOVE: tudo ja esta em `processing`
    await rig.raw.rpush(
        rig.keys.processing, b"\xff\xfe", pause.encode(), b"\xc3", stop.encode(), b"\xef\xbb\xbf"
    )
    rig.new_source()  # processo novo: guard em memoria vazio

    for _ in range(4):
        await rig.h.robot.housekeeping()

    assert rig.h.robot.bot_state is BotState.PAUSED
    assert (await rig.ack(stop_id) or {}).get("result") == "applied"
    assert await rig.raw.llen(rig.keys.processing) == 0
