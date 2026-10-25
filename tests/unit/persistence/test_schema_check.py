"""Checagem de versao do schema na subida da API e do runner (sem banco)."""

from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from api import serve
from engine.infrastructure.persistence import schema_check
from engine.infrastructure.persistence.schema_check import (
    MIGRATIONS_DIR,
    SchemaMismatchError,
    ensure_schema_current,
    expected_head,
)
from engine.runner import paper
from tests.api_support import make_settings


def fake_version(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    async def read(_engine: object) -> str | None:
        return value

    monkeypatch.setattr(schema_check, "database_version", read)


def test_expected_head_is_the_latest_migration_file() -> None:
    latest = sorted(p.stem for p in (MIGRATIONS_DIR / "versions").glob("0*.py"))[-1]
    assert expected_head() == latest.split("_")[0]


async def test_matching_version_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_version(monkeypatch, expected_head())
    await ensure_schema_current(object())  # type: ignore[arg-type]


@pytest.mark.parametrize("current", ["0001", "9999", None])
async def test_diverging_or_unreadable_version_is_refused_with_a_clear_message(
    monkeypatch: pytest.MonkeyPatch, current: str | None
) -> None:
    fake_version(monkeypatch, current)
    with pytest.raises(SchemaMismatchError, match=r"job migrate.*--skip-schema-check"):
        await ensure_schema_current(object())  # type: ignore[arg-type]


class FakeEngine:
    disposed = False

    async def dispose(self) -> None:
        self.disposed = True


def runner_args(tmp_path: Path, *extra: str) -> Any:
    return paper.build_parser().parse_args(["start", "--run-id", "t", "--data-dir", str(tmp_path), *extra])


async def test_runner_refuses_to_start_when_schema_diverges(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    engine = FakeEngine()
    monkeypatch.setattr(paper, "create_engine", lambda _url: engine)
    fake_version(monkeypatch, "0001")
    code = await paper.run_start(runner_args(tmp_path))
    assert code == paper.EXIT_FATAL and engine.disposed


async def test_runner_skip_flag_bypasses_the_check_but_never_in_production(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Stop(Exception):
        pass

    def stop(*_a: object, **_k: object) -> None:
        raise Stop

    engine = FakeEngine()
    monkeypatch.setattr(paper, "create_engine", lambda _url: engine)
    monkeypatch.setattr(paper, "with_redis_password", stop)
    fake_version(monkeypatch, "0001")
    with pytest.raises(Stop):
        await paper.run_start(runner_args(tmp_path, "--skip-schema-check"))
    monkeypatch.setenv("ENVIRONMENT", "prod")
    code = await paper.run_start(runner_args(tmp_path, "--skip-schema-check"))
    assert code == paper.EXIT_USAGE and engine.disposed


def test_api_skip_flag_sets_the_setting_and_production_forbids_it(monkeypatch: pytest.MonkeyPatch) -> None:
    started: dict[str, Any] = {}
    monkeypatch.setattr(serve.uvicorn, "run", lambda *a, **k: started.update(k))
    monkeypatch.setenv("API_SKIP_SCHEMA_CHECK", "false")
    serve.main(["--skip-schema-check"])
    assert serve.os.environ["API_SKIP_SCHEMA_CHECK"] == "true"
    monkeypatch.delenv("API_SKIP_SCHEMA_CHECK")
    assert make_settings(skip_schema_check=True).skip_schema_check is True
    with pytest.raises(ValidationError, match="skip_schema_check"):
        make_settings(
            environment="prod",
            panel_origins=["https://p.example.com"],
            hsts_max_age_seconds=3600,
            database_user="cryptoinvest_api",
            database_password=SecretStr("senha-do-papel-da-api"),
            skip_schema_check=True,
        )
