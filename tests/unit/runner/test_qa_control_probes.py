"""QA: sondas do canal de comandos assinado e da config no runner (xfail strict = bug aberto)."""

import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from engine.runner.command_channel import (
    RedisCommandSource,
    ReplayGuard,
    sign_command,
    verify_envelope,
)
from engine.runner.paper_control import Command, CommandName, CommandQueue
from tests.redis_fakes import FakeRedis
from tests.unit.runner.fakes import Harness
from tests.unit.runner.test_paper_signed_commands import KEY, Setup

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=__import__("datetime").UTC)


def _command(name: CommandName = CommandName.PAUSE, **kw: Any) -> Command:
    return Command(name=name, operator="ana", reason="teste", issued_at=NOW, **kw)


def test_signature_is_checked_before_anything_else_and_tamper_is_rejected() -> None:
    raw = json.loads(sign_command(KEY, _command(), command_id="abcdefgh1", signed_at=NOW))
    raw["payload"]["reason"] = "outro"
    out = verify_envelope(json.dumps(raw), KEY, NOW, ReplayGuard())
    assert out.discarded == "bad_signature"


def test_truncated_envelope_is_malformed() -> None:
    raw = sign_command(KEY, _command(), command_id="abcdefgh1", signed_at=NOW)
    assert verify_envelope(raw[:-5], KEY, NOW, ReplayGuard()).discarded == "malformed"


@pytest.mark.parametrize("skew", [timedelta(seconds=61), timedelta(seconds=-61)])
def test_clock_skew_beyond_window_is_rejected(skew: timedelta) -> None:
    raw = sign_command(KEY, _command(), command_id="abcdefgh1", signed_at=NOW + skew)
    assert verify_envelope(raw, KEY, NOW, ReplayGuard()).discarded in {"stale", "future"}


def test_non_ascii_signature_is_discarded_not_raised() -> None:
    raw = json.dumps({"payload": {"id": "abcdefgh1"}, "sig": "é" * 64})
    assert verify_envelope(raw, KEY, NOW, ReplayGuard()).discarded == "bad_signature"


def test_deeply_nested_json_is_discarded_not_raised() -> None:
    raw = "[" * 3000 + "]" * 3000
    assert verify_envelope(raw, KEY, NOW, ReplayGuard()).discarded == "malformed"


async def test_poison_envelope_does_not_lose_a_legit_emergency_stop(fake_redis: FakeRedis) -> None:
    source = RedisCommandSource(fake_redis, KEY)
    good = sign_command(KEY, _command(CommandName.EMERGENCY_STOP), command_id="emergency0001", signed_at=NOW)
    poison = json.dumps({"payload": {"id": "x"}, "sig": "é" * 64})
    await fake_redis.rpush(source._keys.commands, good, poison)

    outcomes = await source.poll(NOW)

    assert [o.command_id for o in outcomes if o.command is not None] == ["emergency0001"]


async def test_resend_of_a_claimed_nonce_is_discarded_as_replay(fake_redis: FakeRedis) -> None:
    source = RedisCommandSource(fake_redis, KEY)
    command = _command(CommandName.EMERGENCY_STOP)
    for _ in range(2):
        await fake_redis.rpush(
            source._keys.commands, sign_command(KEY, command, command_id="emergency0002", signed_at=NOW)
        )
    first = await source.poll(NOW)
    assert first[0].command is not None and first[1].discarded == "replay"
    assert await source.commit(first[0]) is True
    await source.release(first[1])
    assert await RedisCommandSource(fake_redis, KEY).poll(NOW) == []


@pytest.fixture
async def setup(fake_redis: FakeRedis, tmp_path: Path) -> Setup:
    return Setup(await Harness().start(), fake_redis, tmp_path)


async def test_refused_apply_config_is_not_acknowledged_as_applied(setup: Setup) -> None:
    robot = setup.h.robot
    await setup.send(CommandName.APPLY_CONFIG, params={"risk": {"risk_per_trade": "0.002"}})
    await robot.housekeeping()
    increase = await setup.send(CommandName.APPLY_CONFIG, params={"risk": {"risk_per_trade": "0.004"}})
    await robot.housekeeping()

    assert robot.risk.risk_per_trade == Decimal("0.002")
    ack = await setup.ack(increase)
    assert ack["result"] == "refused" and "aumenta o risco" in ack["detail"]


async def test_reduction_bundled_with_relative_increase_is_applied_per_field(setup: Setup) -> None:
    """Cenario: v2 (pendente) subiu risk_per_trade; v3 volta p/ 0,006 (queda vs v2, alta vs robo) e reduz
    max_daily_loss. A reducao vale; so o aumento relativo ao robo e recusado, e o ack diz 'parcial'."""
    robot = setup.h.robot
    patch = {"risk_per_trade": "0.006", "max_daily_loss": "0.008"}
    command_id = await setup.send(CommandName.APPLY_CONFIG, params={"risk": patch})
    await robot.housekeeping()
    assert robot.risk.max_daily_loss == Decimal("0.008")
    assert robot.risk.risk_per_trade == Decimal("0.005")
    ack = await setup.ack(command_id)
    assert ack["result"] == "partially_applied" and "risk_per_trade" in ack["detail"]


async def test_emergency_stop_never_expires_but_liquidate_does(tmp_path: Path) -> None:
    queue = CommandQueue(tmp_path)
    old = NOW - timedelta(days=3)
    queue.send(Command(name=CommandName.EMERGENCY_STOP, operator="a", reason="r", issued_at=old))
    queue.send(
        Command(
            name=CommandName.EMERGENCY_STOP,
            operator="a",
            reason="r",
            liquidate=True,
            issued_at=old + timedelta(seconds=1),
        )
    )
    names = [(c.name, c.liquidate) for _, c in queue.pending(NOW)]
    assert names == [(CommandName.EMERGENCY_STOP, False)]
