"""Politicas do throttle: chave por /64, mapa lotado de bloqueios (fail-closed), epoca de desbloqueio, HashGate."""

import asyncio

import pytest

from api.errors import TooManyRequests
from api.security.hashgate import HashGate
from api.security.throttle import SATURATED_RETRY_SECONDS, FailureThrottle, LockEpochs, throttle_ip


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.mark.parametrize(
    ("raw", "key"),
    [
        ("203.0.113.9", "203.0.113.9"),
        ("::ffff:203.0.113.9", "203.0.113.9"),
        ("2001:db8:abcd:12::1", "2001:db8:abcd:12::/64"),
        ("2001:db8:abcd:12:ffff:ffff:ffff:ffff", "2001:db8:abcd:12::/64"),
        ("2001:db8:abcd:13::1", "2001:db8:abcd:13::/64"),
        (None, "unknown"),
    ],
)
def test_throttle_key_uses_the_ipv6_prefix_and_the_whole_ipv4(raw: str | None, key: str) -> None:
    assert throttle_ip(raw) == key


def test_full_map_of_active_locks_refuses_new_keys_instead_of_letting_them_through() -> None:
    clock = Clock()
    throttle = FailureThrottle(threshold=2, max_keys=3, clock=clock)
    for i in range(3):
        throttle.register_failure(f"k{i}")
        throttle.register_failure(f"k{i}")
    assert all(throttle.retry_after(f"k{i}") > 0 for i in range(3))
    assert throttle.retry_after("novo") == SATURATED_RETRY_SECONDS
    assert throttle.saturated_refusals == 1
    clock.now += 3600 * 2
    assert throttle.retry_after("novo") == 0


def test_a_full_map_never_evicts_an_active_lock() -> None:
    clock = Clock()
    throttle = FailureThrottle(threshold=2, max_keys=3, clock=clock)
    for i in range(3):
        throttle.register_failure(f"k{i}")
        throttle.register_failure(f"k{i}")
    throttle.register_failure("novo")
    assert all(throttle.retry_after(f"k{i}") > 0 for i in range(3))


def test_lock_epoch_change_is_reported_once_and_rechecks_are_spaced() -> None:
    clock = Clock()
    epochs = LockEpochs(recheck_seconds=5.0, clock=clock)
    assert epochs.changed("u", 0) is False
    assert epochs.changed("u", 0) is False
    assert epochs.changed("u", 1) is True
    assert epochs.changed("u", 1) is False
    assert epochs.due("u") is True
    assert epochs.due("u") is False
    clock.now += 5
    assert epochs.due("u") is True


async def test_hash_gate_counts_and_logs_saturation() -> None:
    gate = HashGate(1, 0.05)
    release = asyncio.Event()

    async def hold() -> None:
        async with gate.slot():
            await release.wait()

    holder = asyncio.create_task(hold())
    await asyncio.sleep(0.01)
    with pytest.raises(TooManyRequests):
        async with gate.slot():
            pass
    assert gate.rejected == 1 and gate.peak == 1 and gate.in_flight == 1
    release.set()
    await holder
    assert gate.in_flight == 0


def test_recovery_key_validation_names_the_offending_field() -> None:
    from pydantic import SecretStr

    from tests.api_support import make_settings

    with pytest.raises(ValueError, match="recovery_hmac_key precisa"):
        make_settings(recovery_hmac_key=SecretStr("curta"))
    with pytest.raises(ValueError, match="command_hmac_key precisa"):
        make_settings(command_hmac_key=SecretStr("curta"))


def test_production_warns_while_legacy_recovery_hashes_are_accepted(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from pydantic import SecretStr

    from tests.api_support import make_settings

    make_settings(
        environment="prod",
        cookie_secure=True,
        panel_origins=["https://painel.exemplo.com"],
        hsts_max_age_seconds=3600,
        database_user="cryptoinvest_api",
        database_password=SecretStr("senha-do-papel-da-api"),
        recovery_hmac_key=SecretStr("r" * 40),
        trust_forwarded_for=True,
        trusted_proxies=["10.0.0.0/8"],
    )
    assert "recovery_legacy_hashes_enabled_in_production" in capsys.readouterr().out
