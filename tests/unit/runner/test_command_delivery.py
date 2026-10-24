"""Entrega at-least-once, acks assinados, limite de descartes e resultados explicitos dos comandos."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from engine.runner.command_channel import (
    MAX_QUEUE_LENGTH,
    DiscardLimiter,
    RedisCommandSource,
    sign_command,
)
from engine.runner.paper_control import Command, CommandName
from engine.runner.signing import open_ack, open_event, wrap_event
from tests.redis_fakes import FakeRedis
from tests.unit.runner.fakes import Harness
from tests.unit.runner.test_paper_signed_commands import KEY, Setup

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
async def setup(fake_redis: FakeRedis, tmp_path: Any) -> Setup:
    return Setup(await Harness().start(), fake_redis, tmp_path)


def _envelope(command_id: str, name: CommandName = CommandName.PAUSE) -> str:
    command = Command(name=name, operator="ana", reason="teste", issued_at=NOW)
    return sign_command(KEY, command, command_id=command_id, signed_at=NOW)


async def test_envelope_stays_in_processing_until_it_is_durable(fake_redis: FakeRedis) -> None:
    source = RedisCommandSource(fake_redis, KEY)
    await fake_redis.rpush(source._keys.commands, _envelope("emergency0001", CommandName.EMERGENCY_STOP))

    [outcome] = await source.poll(NOW)

    assert fake_redis.lists[source._keys.commands] == []
    assert fake_redis.lists[source._keys.processing] == [outcome.raw]
    restarted = RedisCommandSource(fake_redis, KEY)
    [again] = await restarted.poll(NOW)
    assert again.command is not None and again.command_id == "emergency0001"
    assert source._keys.nonce("emergency0001") not in fake_redis.values, "nonce so apos gravar localmente"


async def test_a_crash_between_pop_and_local_write_does_not_lose_the_command(setup: Setup) -> None:
    robot = setup.h.robot
    command_id = await setup.send(CommandName.EMERGENCY_STOP)
    real_send = robot.commands.send  # type: ignore[union-attr]

    def explode(*args: Any, **kwargs: Any) -> Any:
        raise OSError("disco cheio")

    robot.commands.send = explode  # type: ignore[method-assign,union-attr]
    await robot.housekeeping()
    assert robot.bot_state.value == "RUNNING"

    robot.commands.send = real_send  # type: ignore[method-assign,union-attr]
    await robot.housekeeping()
    assert robot.bot_state.value == "PAUSED"
    assert (await setup.ack(command_id))["result"] == "applied"


async def test_replay_after_the_command_was_already_executed_is_not_run_twice(setup: Setup) -> None:
    robot = setup.h.robot
    command = Command(name=CommandName.PAUSE, operator="ana", reason="x", issued_at=robot.clock.now())
    raw = sign_command(KEY, command, command_id="same00000001", signed_at=robot.clock.now())
    await setup.redis.rpush(setup.source._keys.commands, raw)
    await robot.housekeeping()
    assert (await setup.ack("same00000001"))["result"] == "applied"

    restarted = RedisCommandSource(setup.redis, KEY)
    robot.signed_commands = restarted
    await setup.redis.rpush(restarted._keys.commands, raw)
    await robot.housekeeping()

    assert (await setup.ack("same00000001"))["result"] == "applied", "a repeticao nao sobrescreve o resultado"
    assert setup.audit.actions() == ["paper_command_discarded"]


async def test_acks_are_signed_and_forged_ones_are_rejected(fake_redis: FakeRedis) -> None:
    source = RedisCommandSource(fake_redis, KEY)
    await source.ack("abcdefgh1", "applied", "ok")
    stored = fake_redis.values[source._keys.ack("abcdefgh1")]

    assert open_ack(KEY, "abcdefgh1", stored) is not None
    assert open_ack(KEY, "outroid001", stored) is None, "o ack so vale para o proprio id"
    assert open_ack(b"x" * 48, "abcdefgh1", stored) is None
    forged = json.dumps({"result": "applied", "detail": "", "at": NOW.isoformat()})
    assert open_ack(KEY, "abcdefgh1", forged) is None
    assert open_ack(KEY, "abcdefgh1", json.dumps({"sig": "é" * 64})) is None


def test_events_are_signed_per_kind() -> None:
    raw = wrap_event(KEY, "state", {"state": "RUNNING"})
    assert open_event(KEY, "state", raw) == {"state": "RUNNING"}
    assert open_event(KEY, "heartbeat", raw) is None, "assinatura de um tipo nao vale em outro"
    assert open_event(b"y" * 48, "state", raw) is None
    assert open_event(KEY, "state", json.dumps({"payload": {"state": "RUNNING"}, "sig": "00"})) is None
    assert open_event(KEY, "state", "[" * 5000) is None


async def test_a_backlog_above_the_cap_is_drained_so_junk_cannot_starve_real_commands(
    fake_redis: FakeRedis,
) -> None:
    source = RedisCommandSource(fake_redis, KEY)
    junk = [f"lixo-{i}" for i in range(MAX_QUEUE_LENGTH + 50)]
    await fake_redis.rpush(
        source._keys.commands, *junk, _envelope("emergency0009", CommandName.EMERGENCY_STOP)
    )

    outcomes = await source.poll(NOW)

    assert len(outcomes) <= MAX_QUEUE_LENGTH * 2
    assert len(fake_redis.lists[source._keys.commands]) < len(junk) + 1
    assert all(o.discarded for o in outcomes[:-1])


def test_discard_audit_is_rate_limited_and_aggregated_per_minute() -> None:
    limiter = DiscardLimiter(per_minute=2)
    admitted = [limiter.admit("bad_signature", NOW) for _ in range(5)]
    assert admitted == [True, True, False, False, False]
    assert limiter.flush(NOW) is None
    assert limiter.flush(NOW + timedelta(minutes=1)) == {"bad_signature": 3}
    assert limiter.flush(NOW + timedelta(minutes=1)) is None


async def test_flood_of_bad_envelopes_writes_a_bounded_number_of_audit_rows(setup: Setup) -> None:
    await setup.redis.rpush(setup.source._keys.commands, *[f"lixo-{i}" for i in range(60)])
    await setup.h.robot.housekeeping()
    assert len(setup.audit.actions()) <= 6


async def test_refused_reactivate_and_resume_are_never_confirmed_as_applied(setup: Setup) -> None:
    robot = setup.h.robot
    reactivate = await setup.send(CommandName.REACTIVATE)
    resume = await setup.send(CommandName.RESUME)
    await robot.housekeeping()

    assert (await setup.ack(reactivate))["result"] == "refused"
    assert (await setup.ack(resume))["result"] == "refused"


async def test_expired_signed_command_gets_an_expired_ack(setup: Setup) -> None:
    robot = setup.h.robot
    old = robot.clock.now() - timedelta(minutes=30)
    command_id = await setup.send(CommandName.PAUSE, issued_at=old)
    await robot.housekeeping()
    assert (await setup.ack(command_id))["result"] == "expired"
