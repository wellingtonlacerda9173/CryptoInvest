"""Endurecimento de seguranca da fundacao da API: throttle, fila de hashes, usuario, rotulos, proxy, CLI."""

import asyncio
import os
import types
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr, TypeAdapter, ValidationError
from starlette.requests import Request

from api.binance_keys import KeyVerificationError, _permissions_from
from api.cli import (
    MASTER_KEYS_FILE,
    RECOVERY_KEY_FILE,
    SESSION_SECRET_FILE,
    _write_secret,
    append_master_key,
    init_secrets,
)
from api.config import ApiSettings, api_database_url
from api.deps import client_ip
from api.errors import TooManyRequests
from api.schemas import Label
from api.security.crypto import KeyRing, generate_key_line
from api.security.hashgate import HashGate
from api.security.throttle import (
    LOCKOUT_THRESHOLD,
    AuditSampler,
    FailureThrottle,
    UserPressure,
    lockout_seconds,
)
from api.security.tokens import (
    hash_activation_token,
    hash_recovery_code,
    new_activation_token,
    username_tag,
)
from api.services.users import USERNAME_PATTERN, is_valid_username, normalize_username
from tests.api_support import make_settings


class _Tick:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


# ----------------------------------------------------------------------------- throttle


def test_lock_starts_exactly_at_the_threshold_and_not_before() -> None:
    throttle = FailureThrottle(threshold=3, clock=_Tick())
    assert [throttle.register_failure("k") for _ in range(3)] == [0, 0, 30]
    assert lockout_seconds(LOCKOUT_THRESHOLD - 1) == 0
    assert lockout_seconds(LOCKOUT_THRESHOLD) == 30


def test_refund_releases_the_lock_it_caused_but_keeps_earlier_failures() -> None:
    throttle = FailureThrottle(threshold=2, clock=_Tick())
    throttle.register_failure("k")
    assert throttle.register_failure("k") == 30
    throttle.refund("k")
    assert throttle.retry_after("k") == 0
    assert throttle.register_failure("k") == 30, "a falha anterior continua contada"
    throttle.refund("ausente")


def test_events_older_than_the_window_stop_counting_at_the_boundary() -> None:
    clock = _Tick()
    throttle = FailureThrottle(threshold=2, window_seconds=60, clock=clock)
    throttle.register_failure("k")
    clock.now += 60
    assert throttle.register_failure("k") > 0, "evento exatamente na borda da janela ainda conta"
    clock.now += 61
    assert throttle.register_failure("k") == 0


def test_per_key_memory_is_bounded_and_lock_stays_capped() -> None:
    throttle = FailureThrottle(threshold=2, clock=_Tick())
    for _ in range(500):
        throttle.register_failure("k")
    assert len(throttle._failures["k"]) <= 2 + 16
    assert throttle.retry_after("k") <= 3601


def test_full_map_of_active_locks_never_evicts_one() -> None:
    clock = _Tick()
    throttle = FailureThrottle(threshold=1, max_keys=3, clock=clock)
    for name in ("a", "b", "c"):
        assert throttle.register_failure(name) > 0
    assert throttle.register_failure("novo") == 0
    assert all(throttle.retry_after(name) > 0 for name in ("a", "b", "c"))
    assert len(throttle._failures) == 3


def test_user_pressure_only_delays_and_is_capped() -> None:
    pressure = UserPressure(free_attempts=2, base_seconds=0.5, cap_seconds=2.0, clock=_Tick())
    assert pressure.delay_seconds("u") == 0.0
    delays = []
    for _ in range(8):
        pressure.register_failure("u")
        delays.append(pressure.delay_seconds("u"))
    assert delays[:4] == [0.0, 0.5, 1.0, 2.0]
    assert delays == sorted(delays) and max(delays) == 2.0
    assert pressure.count("u") == 8
    pressure.clear("u")
    assert pressure.delay_seconds("u") == 0.0


def test_user_pressure_window_and_capacity() -> None:
    clock = _Tick()
    pressure = UserPressure(window_seconds=60, max_keys=2, clock=clock)
    pressure.register_failure("a")
    clock.now += 61
    assert pressure.count("a") == 0
    pressure.register_failure("a")
    pressure.register_failure("b")
    assert pressure.register_failure("c") == 0, "cheio: chave nova nao e contada (memoria limitada)"


def test_audit_sampler_emits_once_per_window_per_key() -> None:
    clock = _Tick()
    sampler = AuditSampler(window_seconds=60, max_keys=2, clock=clock)
    assert sampler.should_emit("a") and not sampler.should_emit("a")
    assert sampler.should_emit("b")
    assert not sampler.should_emit("c"), "cheio de chaves vivas: descarta em vez de crescer"
    clock.now += 60
    assert sampler.should_emit("a")


# ----------------------------------------------------------------------------- fila de hashes


async def test_hash_gate_limits_concurrency_and_rejects_fast_when_full() -> None:
    gate = HashGate(max_concurrent=2, queue_timeout_seconds=0.05)
    release = asyncio.Event()

    async def hold() -> None:
        async with gate.slot():
            await release.wait()

    holders = [asyncio.create_task(hold()) for _ in range(2)]
    await asyncio.sleep(0.01)
    assert gate.in_flight == 2
    with pytest.raises(TooManyRequests) as refused:
        async with gate.slot():
            pytest.fail("nao deveria entrar")
    assert refused.value.retry_after == 1
    release.set()
    await asyncio.gather(*holders)
    assert gate.in_flight == 0 and gate.peak == 2
    async with gate.slot():
        assert gate.in_flight == 1


# ----------------------------------------------------------------------------- nome de usuario


@pytest.mark.parametrize("raw", ["Kleber.silva", "ｋleber", "aná.silva", "ana.silva​", "ànà.silva"])
def test_non_ascii_never_becomes_a_valid_username(raw: str) -> None:
    assert not is_valid_username(normalize_username(raw))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("  Ana.Silva ", "ana.silva"), ("JOAO@EXEMPLO.COM", "joao@exemplo.com"), ("a-b_c", "a-b_c")],
)
def test_ascii_usernames_are_casefolded(raw: str, expected: str) -> None:
    assert normalize_username(raw) == expected


@pytest.mark.parametrize("name", ["ab", "a" * 65, "-abc", "abc\n", "ab c", "abc/../x"])
def test_username_rules_are_strict(name: str) -> None:
    assert not is_valid_username(name)
    assert USERNAME_PATTERN.fullmatch(name) is None


def test_username_boundaries_are_accepted() -> None:
    assert is_valid_username("abc") and is_valid_username("a" * 64) and is_valid_username("0.a-@")


# ----------------------------------------------------------------------------- rotulo


@pytest.mark.parametrize(
    "label",
    [
        "Conta\x00principal",
        "Conta\nprincipal",
        "Conta‮principal",
        "Conta​principal",
        "Conta⁦principal",
        "Conta principal",
        "⠀⠀",
        "aㅤb",
        "a﻿b",
        "a" * 65,
        "   ",
    ],
)
def test_label_rejects_control_bidi_and_invisible_characters(label: str) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(Label).validate_python(label)


def test_label_accepts_ordinary_text_including_accents() -> None:
    adapter = TypeAdapter(Label)
    assert adapter.validate_python("  Conta principal ") == "Conta principal"
    assert adapter.validate_python("Operacao - Cripto/Joao 2") == "Operacao - Cripto/Joao 2"
    assert adapter.validate_python("a" * 64) == "a" * 64


# ----------------------------------------------------------------------------- verificador Binance


FLAGS = {"enableReading": True, "enableSpotAndMarginTrading": True, "enableWithdrawals": False}


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [1, 2],
        "texto",
        {},
        {k: v for k, v in FLAGS.items() if k != "enableWithdrawals"},
        {k: v for k, v in FLAGS.items() if k != "enableReading"},
        {k: v for k, v in FLAGS.items() if k != "enableSpotAndMarginTrading"},
        {**FLAGS, "enableWithdrawals": None},
        {**FLAGS, "enableWithdrawals": "false"},
        {**FLAGS, "enableWithdrawals": 0},
        {**FLAGS, "enableReading": "true"},
    ],
)
def test_permissions_fail_closed_on_anything_but_explicit_booleans(payload: Any) -> None:
    with pytest.raises(KeyVerificationError) as raised:
        _permissions_from(payload)
    assert "A resposta da Binance" in str(raised.value)
    assert str(payload) not in str(raised.value) or payload in ({}, None)


def test_permissions_parse_a_complete_payload() -> None:
    permissions = _permissions_from({**FLAGS, "enableWithdrawals": True, "ipRestrict": True})
    assert permissions.can_read and permissions.can_trade and permissions.can_withdraw
    assert permissions.ip_restricted is True and permissions.can_transfer is False
    assert _permissions_from(FLAGS).ip_restricted is None


# ----------------------------------------------------------------------------- config e proxy


def _prod(**overrides: Any) -> ApiSettings:
    values: dict[str, Any] = {
        "environment": "prod",
        "panel_origins": ["https://painel.exemplo.com"],
        "hsts_max_age_seconds": 3600,
        "database_user": "cryptoinvest_api",
        "database_password": SecretStr("senha-do-papel-da-api"),
        "session_secret": SecretStr("x" * 40),
        "master_keys": SecretStr("1:" + "A" * 43 + "="),
    }
    values.update(overrides)
    return ApiSettings(**values)


def test_production_requires_trusted_proxies_when_forwarded_for_is_trusted() -> None:
    with pytest.raises(ValueError, match="trusted_proxies"):
        _prod(trust_forwarded_for=True)
    assert _prod(trust_forwarded_for=True, trusted_proxies=["10.0.0.0/8"]).proxy_networks
    assert _prod().trusted_proxies == []


def test_invalid_trusted_proxy_is_refused() -> None:
    with pytest.raises(ValueError, match="proxy confiavel"):
        make_settings(trusted_proxies=["nao-e-ip"])


def _request(peer: str | None, forwarded: str | None = None) -> Any:
    raw = [(b"x-forwarded-for", forwarded.encode())] if forwarded is not None else []
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": raw,
        "client": (peer, 5000) if peer else None,
    }
    return Request(scope)


def _container(**overrides: Any) -> Any:
    return types.SimpleNamespace(settings=make_settings(**overrides))


def test_forwarded_for_is_ignored_unless_enabled() -> None:
    assert client_ip(_request("10.0.0.5", "6.6.6.6"), _container()) == "10.0.0.5"


def test_forwarded_for_from_an_untrusted_peer_is_ignored() -> None:
    container = _container(trust_forwarded_for=True, trusted_proxies=["10.0.0.0/8"])
    assert client_ip(_request("203.0.113.7", "6.6.6.6"), container) == "203.0.113.7"


def test_forwarded_for_takes_the_first_untrusted_hop_from_the_right() -> None:
    container = _container(trust_forwarded_for=True, trusted_proxies=["10.0.0.0/8", "172.16.0.1"])
    spoofed = "6.6.6.6, 198.51.100.4, 10.1.1.1, 172.16.0.1"
    assert client_ip(_request("10.0.0.5", spoofed), container) == "198.51.100.4"
    assert client_ip(_request("10.0.0.5"), container) == "10.0.0.5"
    assert client_ip(_request("10.0.0.5", "10.2.2.2"), container) == "10.0.0.5"


def test_without_a_proxy_list_dev_keeps_the_last_hop_behavior() -> None:
    container = _container(trust_forwarded_for=True)
    assert client_ip(_request("10.0.0.5", "6.6.6.6, 9.9.9.9"), container) == "9.9.9.9"
    assert client_ip(_request(None), container) is None


def test_api_database_url_uses_the_api_role_when_configured() -> None:
    from sqlalchemy import make_url

    base = make_url("postgresql+asyncpg://robo:senha-robo@db:5432/cryptoinvest")
    assert api_database_url(make_settings(), base) == base
    url = api_database_url(
        make_settings(database_user="cryptoinvest_api", database_password=SecretStr("senha-api")), base
    )
    assert url.username == "cryptoinvest_api" and url.password == "senha-api" and url.host == "db"


# ----------------------------------------------------------------------------- tokens


def test_activation_tokens_are_unique_high_entropy_and_hashed_with_a_domain() -> None:
    first, second = new_activation_token(), new_activation_token()
    assert first != second and len(first) >= 32
    assert hash_activation_token(first) != hash_activation_token(second)
    import hashlib

    assert hash_activation_token(first) != hashlib.sha256(first.encode()).hexdigest()


def test_username_tag_is_stable_keyed_and_short() -> None:
    assert username_tag("segredo-1", "ana") == username_tag("segredo-1", "ana")
    assert username_tag("segredo-1", "ana") != username_tag("segredo-2", "ana")
    assert username_tag("segredo-1", "ana") != username_tag("segredo-1", "ana2")
    assert len(username_tag("segredo-1", "x" * 10_000)) == 16


def test_recovery_hash_depends_on_the_secret_and_ignores_formatting() -> None:
    assert hash_recovery_code("a", "abcd-efgh") == hash_recovery_code("a", " ABCD EFGH ")
    assert hash_recovery_code("a", "abcd-efgh") != hash_recovery_code("b", "abcd-efgh")


# ----------------------------------------------------------------------------- CLI de segredos


def test_init_secrets_creates_every_file_without_overwriting(tmp_path: Path) -> None:
    created = init_secrets(tmp_path)
    assert {SESSION_SECRET_FILE, RECOVERY_KEY_FILE, MASTER_KEYS_FILE} <= set(created)
    before = {name: (tmp_path / name).read_bytes() for name in created}
    assert init_secrets(tmp_path) == []
    assert {name: (tmp_path / name).read_bytes() for name in created} == before
    assert (tmp_path / SESSION_SECRET_FILE).read_text().strip() != (
        tmp_path / RECOVERY_KEY_FILE
    ).read_text().strip()


def test_write_secret_refuses_to_overwrite_and_creates_private_file(tmp_path: Path) -> None:
    target = tmp_path / "segredo"
    _write_secret(target, "um")
    with pytest.raises(FileExistsError):
        _write_secret(target, "dois")
    assert target.read_text() == "um"
    if os.name == "posix":
        assert target.stat().st_mode & 0o777 == 0o600


def test_new_master_key_appends_on_its_own_line_and_keeps_the_ring_valid(tmp_path: Path) -> None:
    path = tmp_path / MASTER_KEYS_FILE
    path.write_text(generate_key_line(1) + "\n", encoding="utf-8")
    assert append_master_key(tmp_path) == 2
    assert append_master_key(tmp_path) == 3
    ring = KeyRing.parse(path.read_text(encoding="utf-8"))
    assert sorted(ring.keys) == [1, 2, 3] and ring.active_version == 3
    assert "\n\n" not in path.read_text(encoding="utf-8")
