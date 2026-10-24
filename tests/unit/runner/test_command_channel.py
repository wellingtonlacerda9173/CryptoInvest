"""Canal de comandos assinado: HMAC, carimbo, anti-replay e validacao do corpo."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from engine.runner.command_channel import (
    MAX_ENVELOPE_BYTES,
    MAX_SKEW,
    DiscardCode,
    RedisCommandSource,
    ReplayGuard,
    canonical,
    load_command_key,
    sign_command,
    verify_envelope,
)
from engine.runner.paper_control import Command, CommandName

KEY = b"k" * 40
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
ID = "a1b2c3d4e5f60718"


def command(**overrides: Any) -> Command:
    values: dict[str, Any] = {
        "name": CommandName.PAUSE,
        "operator": "ana",
        "reason": "manutencao",
        "issued_at": NOW,
    }
    return Command.model_validate(values | overrides)


def envelope(cmd: Command | None = None, *, at: datetime = NOW, key: bytes = KEY, cid: str = ID) -> str:
    return sign_command(key, cmd or command(), command_id=cid, signed_at=at)


def test_valid_envelope_yields_the_command_with_params() -> None:
    cmd = command(name=CommandName.APPLY_CONFIG, params={"risk": {"risk_per_trade": "0.004"}})
    outcome = verify_envelope(envelope(cmd), KEY, NOW, ReplayGuard())
    assert outcome.discarded is None and outcome.command == cmd and outcome.command_id == ID


def test_wrong_key_or_tampered_payload_is_discarded() -> None:
    guard = ReplayGuard()
    assert verify_envelope(envelope(key=b"x" * 40), KEY, NOW, guard).discarded == DiscardCode.BAD_SIGNATURE
    raw = json.loads(envelope())
    raw["payload"]["name"] = "emergency-stop"
    assert verify_envelope(json.dumps(raw), KEY, NOW, guard).discarded == DiscardCode.BAD_SIGNATURE
    raw = json.loads(envelope())
    raw["payload"]["liquidate"] = True
    assert verify_envelope(json.dumps(raw), KEY, NOW, guard).discarded == DiscardCode.BAD_SIGNATURE


def test_stale_and_future_stamps_are_discarded() -> None:
    guard = ReplayGuard()
    old = envelope(at=NOW - MAX_SKEW - timedelta(seconds=1), cid="old00000000")
    future = envelope(at=NOW + MAX_SKEW + timedelta(seconds=1), cid="future000000")
    assert verify_envelope(old, KEY, NOW, guard).discarded == DiscardCode.STALE
    assert verify_envelope(future, KEY, NOW, guard).discarded == DiscardCode.FUTURE
    edge = envelope(at=NOW - MAX_SKEW, cid="edge00000000")
    assert verify_envelope(edge, KEY, NOW, guard).command is not None


def test_replay_of_the_same_nonce_is_discarded() -> None:
    guard = ReplayGuard()
    raw = envelope()
    assert verify_envelope(raw, KEY, NOW, guard).command is not None
    assert verify_envelope(raw, KEY, NOW, guard).discarded == DiscardCode.REPLAY


def test_replay_guard_forgets_after_the_ttl_and_is_bounded() -> None:
    guard = ReplayGuard(ttl_seconds=10, max_entries=3)
    assert guard.first_time("a", now=0.0)
    assert not guard.first_time("a", now=5.0)
    assert guard.first_time("a", now=11.0)
    for index in range(10):
        guard.first_time(f"n{index}", now=12.0)
    assert len(guard._seen) <= 4


@pytest.mark.parametrize(
    "raw",
    ["", "nao-e-json", "[]", '{"payload": 1, "sig": "x"}', '{"payload": {}, "sig": 3}', '{"sig": "x"}'],
)
def test_malformed_envelopes_are_discarded_without_raising(raw: str) -> None:
    assert verify_envelope(raw, KEY, NOW, ReplayGuard()).discarded == DiscardCode.MALFORMED


def test_oversized_envelope_is_discarded_before_parsing() -> None:
    assert (
        verify_envelope("x" * (MAX_ENVELOPE_BYTES + 1), KEY, NOW, ReplayGuard()).discarded
        == DiscardCode.TOO_LARGE
    )


def test_naive_timestamp_is_malformed_even_with_a_valid_signature() -> None:
    payload = {"v": 1, "id": ID, "ts": "2026-10-20T12:00:00", "name": "pause", "operator": "a", "reason": "b"}
    import hashlib
    import hmac

    sig = hmac.new(KEY, canonical(payload), hashlib.sha256).hexdigest()
    raw = json.dumps({"payload": payload, "sig": sig})
    assert verify_envelope(raw, KEY, NOW, ReplayGuard()).discarded == DiscardCode.MALFORMED


def test_invalid_command_body_is_discarded() -> None:
    import hashlib
    import hmac

    payload = {
        "v": 1, "id": ID, "ts": NOW.isoformat(), "name": "format-disk", "operator": "ana", "reason": "x",
        "liquidate": False, "issued_at": NOW.isoformat(), "params": {},
    }  # fmt: skip
    sig = hmac.new(KEY, canonical(payload), hashlib.sha256).hexdigest()
    raw = json.dumps({"payload": payload, "sig": sig})
    assert verify_envelope(raw, KEY, NOW, ReplayGuard()).discarded == DiscardCode.INVALID


def test_command_issued_in_the_future_is_discarded() -> None:
    raw = envelope(command(issued_at=NOW + timedelta(minutes=5)))
    assert verify_envelope(raw, KEY, NOW, ReplayGuard()).discarded == DiscardCode.FUTURE


def test_short_id_is_invalid() -> None:
    assert verify_envelope(envelope(cid="abc"), KEY, NOW, ReplayGuard()).discarded == DiscardCode.INVALID


def test_load_command_key_requires_a_long_secret_file(tmp_path: Path) -> None:
    assert load_command_key(None) is None
    assert load_command_key(tmp_path) is None
    (tmp_path / "command_hmac_key").write_text("curta", encoding="utf-8")
    assert load_command_key(tmp_path) is None
    (tmp_path / "command_hmac_key").write_text("s" * 48 + "\n", encoding="utf-8")
    assert load_command_key(tmp_path) == b"s" * 48


async def test_redis_source_pops_verifies_and_blocks_replay_across_restarts(fake_redis: Any) -> None:
    source = RedisCommandSource(fake_redis, KEY)
    await fake_redis.rpush(source._keys.commands, envelope(), envelope(cid="other0000001"), "lixo")
    first = await source.poll(NOW)
    assert [o.discarded for o in first] == [None, None, DiscardCode.MALFORMED]
    assert [await source.commit(o) for o in first[:2]] == [True, True]
    await source.release(first[2])
    assert fake_redis.lists[source._keys.processing] == []
    await fake_redis.rpush(source._keys.commands, envelope())
    restarted = RedisCommandSource(fake_redis, KEY)
    [again] = await restarted.poll(NOW)
    assert again.command is not None and again.discarded is None
    assert await restarted.commit(again) is False, (
        "o nonce ja reivindicado antes do reinicio nao vale de novo"
    )


async def test_redis_source_acks_with_scrubbed_detail_and_survives_redis_errors(fake_redis: Any) -> None:
    source = RedisCommandSource(fake_redis, KEY)
    await source.ack(ID, "failed", "erro token=abcdefghijklmnopqrstuvwxyz0123456789")
    stored = json.loads(await fake_redis.get(source._keys.ack(ID)))
    assert stored["result"] == "failed" and "abcdefghijklmnopqrstuvwxyz0123456789" not in stored["detail"]
    fake_redis.broken = True
    assert await source.poll(NOW) == []
    await source.ack(ID, "applied")
