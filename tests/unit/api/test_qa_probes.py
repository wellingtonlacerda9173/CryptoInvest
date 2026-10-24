"""Sondas unitarias da revisao de seguranca (QA). `xfail(strict=True, reason="BUG: ...")` comprova defeito."""

from pathlib import Path

import pytest
from pydantic import SecretStr

from api.cli import MASTER_KEYS_FILE, append_master_key
from api.config import ApiSettings
from api.security.crypto import KeyRing, generate_key_line
from api.security.throttle import FailureThrottle
from api.services.users import USERNAME_PATTERN, normalize_username


class _Tick:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_throttle_lock_survives_while_map_has_room() -> None:
    throttle = FailureThrottle(threshold=2, max_keys=10, clock=_Tick())
    throttle.register_failure("u:victim")
    assert throttle.register_failure("u:victim") > 0
    assert throttle.retry_after("u:victim") > 0


def test_active_lock_is_not_evicted_by_key_flood() -> None:
    clock = _Tick()
    throttle = FailureThrottle(threshold=2, max_keys=5, clock=clock)
    throttle.register_failure("u:victim")
    assert throttle.register_failure("u:victim") > 0
    for i in range(10):
        clock.now += 1
        throttle.register_failure(f"u:flood{i}")
    assert throttle.retry_after("u:victim") > 0


def test_non_ascii_characters_never_normalize_into_a_valid_username() -> None:
    assert not USERNAME_PATTERN.match(normalize_username("Kleber.silva"))


def test_production_refuses_non_https_exchange_origin() -> None:
    with pytest.raises(ValueError, match="https"):
        ApiSettings(
            environment="prod",
            panel_origins=["https://painel.exemplo.com"],
            hsts_max_age_seconds=3600,
            binance_api_origin="http://127.0.0.1:9999",
            session_secret=SecretStr("x" * 40),
            master_keys=SecretStr("1:" + "A" * 43 + "="),
        )


def test_new_master_key_on_file_without_trailing_newline_keeps_keyring_valid(tmp_path: Path) -> None:
    (tmp_path / MASTER_KEYS_FILE).write_text(generate_key_line(1), encoding="utf-8")

    append_master_key(tmp_path)

    ring = KeyRing.parse((tmp_path / MASTER_KEYS_FILE).read_text(encoding="utf-8"))
    assert sorted(ring.keys) == [1, 2]
