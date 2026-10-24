"""Migracao 0009: raiz unica e FK em config_versions, novos resultados de comando (up/down/check)."""

import asyncio

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import URL, text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration.disposable_db import assert_disposable_database

pytestmark = pytest.mark.integration

VERSION = (
    "INSERT INTO config_versions (parent_id, actor, reason, sections, activation, risk_increase, document, "
    "diff, config_hash, chain_hash) VALUES ({parent}, 'ana', 'motivo', '[]', 'next_start', false, "
    "'{{}}', '[]', 'h', 'c')"
)
COMMAND = (
    "INSERT INTO control_commands (id, name, operator, reason, state, created_at) "
    "VALUES ('{id}', 'apply-config', 'ana', 'x', '{state}', now())"
)


def _config(url: URL) -> Config:
    assert_disposable_database(url)
    config = Config("alembic.ini")
    config.attributes["database_url"] = url
    return config


def _run(url: URL, sql: str) -> list[tuple[object, ...]]:
    async def go() -> list[tuple[object, ...]]:
        engine = create_async_engine(url)
        try:
            async with engine.begin() as conn:
                result = await conn.execute(text(sql))
                return [tuple(r) for r in result] if result.returns_rows else []
        finally:
            await engine.dispose()

    return asyncio.run(go())


def test_up_down_up_and_alembic_check_is_clean(migration_url: URL) -> None:
    config = _config(migration_url)
    command.upgrade(config, "head")
    command.check(config)
    command.downgrade(config, "0008")
    indexes = {
        r[0]
        for r in _run(migration_url, "SELECT indexname FROM pg_indexes WHERE tablename = 'config_versions'")
    }
    assert "uq_config_versions_single_root" not in indexes
    command.upgrade(config, "head")
    command.check(config)


def test_two_roots_and_orphan_parents_are_rejected(migration_url: URL) -> None:
    command.upgrade(_config(migration_url), "head")
    _run(migration_url, VERSION.format(parent="NULL"))
    with pytest.raises(Exception, match="uq_config_versions_single_root"):
        _run(migration_url, VERSION.format(parent="NULL"))
    with pytest.raises(Exception, match="fk_config_versions_parent"):
        _run(migration_url, VERSION.format(parent="999"))
    _run(migration_url, VERSION.format(parent="1"))
    with pytest.raises(Exception, match="uq_config_versions_parent"):
        _run(migration_url, VERSION.format(parent="1"))


def test_new_command_states_are_accepted_and_downgrade_maps_them_back(migration_url: URL) -> None:
    config = _config(migration_url)
    command.upgrade(config, "head")
    for n, state in enumerate(("partially_applied", "refused", "applied")):
        _run(migration_url, COMMAND.format(id=f"{n}" * 32, state=state))
    with pytest.raises(Exception, match="ck_control_commands_state"):
        _run(migration_url, COMMAND.format(id="9" * 32, state="sumiu"))

    command.downgrade(config, "0008")
    states = dict(_run(migration_url, "SELECT id, state FROM control_commands"))  # type: ignore[arg-type]
    assert states == {"0" * 32: "applied", "1" * 32: "failed", "2" * 32: "applied"}
    with pytest.raises(Exception, match="ck_control_commands_state"):
        _run(migration_url, COMMAND.format(id="8" * 32, state="refused"))
    command.upgrade(config, "head")
