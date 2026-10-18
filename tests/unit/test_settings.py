from pathlib import Path

import pytest
from pydantic import ValidationError
from structlog.testing import capture_logs

from engine.infrastructure.config.logging import _redact
from engine.infrastructure.config.settings import MissingSecretError, Settings, load_settings


def test_password_is_read_from_secret_file_and_not_exposed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "database_password").write_text("s3cr3t-value")
    settings = Settings(_secrets_dir=tmp_path)  # type: ignore[call-arg]
    assert settings.database_password.get_secret_value() == "s3cr3t-value"
    assert "s3cr3t-value" not in repr(settings)
    assert "s3cr3t-value" not in str(settings.database_url)


def test_missing_password_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATABASE_PASSWORD", raising=False)
    with pytest.raises(ValidationError):
        Settings(_secrets_dir=tmp_path)  # type: ignore[call-arg]


def test_log_redaction() -> None:
    event = _redact(
        None, "info", {"event": "x", "database_password": "p", "api_key": "k", "symbol": "BTC/USDT"}
    )
    assert event["database_password"] == "***"  # noqa: S105
    assert event["api_key"] == "***"
    assert event["symbol"] == "BTC/USDT"


def test_secret_from_environment_is_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_PASSWORD", "from-env")
    with pytest.raises(ValidationError):
        Settings(_secrets_dir=tmp_path)  # type: ignore[call-arg]


def test_secret_from_dotenv_is_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("DATABASE_PASSWORD=from-dotenv\nDATABASE_HOST=db\n")
    with pytest.raises(ValidationError):
        Settings(_secrets_dir=tmp_path)  # type: ignore[call-arg]


@pytest.mark.parametrize("name", ["DATABASE_PASSWORD", "BINANCE_API_KEY", "BINANCE_API_SECRET"])
def test_ignored_secret_env_var_logs_warning_without_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "database_password").write_text("from-file")
    monkeypatch.setenv(name, "leaked-value-123")

    with capture_logs() as logs:
        Settings(_secrets_dir=tmp_path)  # type: ignore[call-arg]

    warnings = [e for e in logs if e["event"] == "secret_from_environment_ignored"]
    assert [e["field"] for e in warnings] == [name.lower()]
    assert "leaked-value-123" not in repr(logs)


def test_ignored_secret_in_dotenv_logs_warning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "database_password").write_text("from-file")
    (tmp_path / ".env").write_text("BINANCE_API_KEY=leaked-dotenv\n")

    with capture_logs() as logs:
        Settings(_secrets_dir=tmp_path)  # type: ignore[call-arg]

    assert [e["field"] for e in logs if e["event"] == "secret_from_environment_ignored"] == [
        "binance_api_key"
    ]
    assert "leaked-dotenv" not in repr(logs)


def test_no_warning_without_secret_env_vars(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    for name in ("DATABASE_PASSWORD", "BINANCE_API_KEY", "BINANCE_API_SECRET"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "database_password").write_text("from-file")

    with capture_logs() as logs:
        Settings(_secrets_dir=tmp_path)  # type: ignore[call-arg]

    assert not [e for e in logs if e["event"] == "secret_from_environment_ignored"]


def test_load_settings_reports_clear_error_when_password_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SECRETS_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_PASSWORD", "from-env")

    with pytest.raises(MissingSecretError, match="database_password") as info:
        load_settings()

    assert "from-env" not in str(info.value)
