"""A API le a chave de comandos do arquivo `command_hmac_key`, o MESMO que o runner usa (sem o prefixo api_)."""

import base64
from pathlib import Path

import pytest

from api.config import load_api_settings


def _secrets(tmp_path: Path, command_key: str | None) -> None:
    (tmp_path / "api_session_secret").write_text("s" * 48, encoding="utf-8")
    (tmp_path / "api_master_keys").write_text(
        f"1:{base64.b64encode(bytes(range(32))).decode()}", encoding="utf-8"
    )
    (tmp_path / "database_password").write_text("x" * 20, encoding="utf-8")
    if command_key is not None:
        (tmp_path / "command_hmac_key").write_text(command_key + "\n", encoding="utf-8")


def test_the_api_picks_up_the_runner_shared_command_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _secrets(tmp_path, "k" * 48)
    monkeypatch.setenv("SECRETS_DIR", str(tmp_path))
    settings = load_api_settings()
    assert settings.command_hmac_key is not None
    assert settings.command_hmac_key.get_secret_value() == "k" * 48


def test_without_the_file_the_signed_channel_stays_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _secrets(tmp_path, None)
    monkeypatch.setenv("SECRETS_DIR", str(tmp_path))
    assert load_api_settings().command_hmac_key is None
