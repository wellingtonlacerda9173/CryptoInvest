"""Migracao 0007: versoes de configuracao (append-only), fila duravel de comandos e jobs de backtest."""

import asyncio

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import URL, text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration.disposable_db import assert_disposable_database

pytestmark = pytest.mark.integration

NEW_TABLES = {"config_versions", "control_commands", "backtest_jobs"}


def _config(url: URL) -> Config:
    assert_disposable_database(url)
    config = Config("alembic.ini")
    config.attributes["database_url"] = url
    return config


async def _scalar_rows(url: URL, sql: str) -> set[str]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            return {row[0] for row in await conn.execute(text(sql))}
    finally:
        await engine.dispose()


def _tables(url: URL) -> set[str]:
    return asyncio.run(_scalar_rows(url, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))


def test_up_down_up_and_alembic_check_is_clean(migration_url: URL) -> None:
    config = _config(migration_url)
    command.upgrade(config, "head")
    assert _tables(migration_url) >= NEW_TABLES
    command.check(config)

    command.downgrade(config, "0006")
    remaining = _tables(migration_url)
    assert (
        not NEW_TABLES & remaining and {"users", "exchange_credentials", "trades", "audit_log"} <= remaining
    )

    command.upgrade(config, "head")
    assert _tables(migration_url) >= NEW_TABLES
    command.check(config)


def test_config_versions_are_protected_by_append_only_triggers(migration_url: URL) -> None:
    command.upgrade(_config(migration_url), "head")
    triggers = asyncio.run(
        _scalar_rows(
            migration_url,
            "SELECT tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid WHERE c.relname = 'config_versions'",
        )
    )
    assert {"config_versions_append_only", "config_versions_no_truncate"} <= triggers


def test_constraints_reject_inconsistent_rows(migration_url: URL) -> None:
    command.upgrade(_config(migration_url), "head")

    async def attempt(sql: str) -> None:
        engine = create_async_engine(migration_url)
        try:
            async with engine.begin() as conn:
                await conn.execute(text(sql))
        finally:
            await engine.dispose()

    version = (
        "INSERT INTO config_versions (parent_id, actor, reason, sections, activation, risk_increase, document, "
        "diff, config_hash, chain_hash) VALUES ({parent}, '{actor}', '{reason}', '[]', '{activation}', false, "
        "'{{}}', '[]', 'h', 'c')"
    )
    asyncio.run(attempt(version.format(parent="NULL", actor="ana", reason="inicio", activation="immediate")))
    for bad, pattern in (
        (
            version.format(parent="NULL", actor="ana", reason="x", activation="whenever"),
            "ck_config_versions_activation",
        ),
        (
            version.format(parent="NULL", actor=" ", reason="x", activation="immediate"),
            "ck_config_versions_identified",
        ),
        (
            version.format(parent="NULL", actor="ana", reason=" ", activation="immediate"),
            "ck_config_versions_identified",
        ),
        (version.format(parent="1", actor="a", reason="b", activation="next_start"), None),
    ):
        if pattern is None:
            asyncio.run(attempt(bad))
            with pytest.raises(Exception, match="uq_config_versions_parent"):
                asyncio.run(attempt(bad))
        else:
            with pytest.raises(Exception, match=pattern):
                asyncio.run(attempt(bad))
    with pytest.raises(Exception, match="config_versions"):
        asyncio.run(attempt("UPDATE config_versions SET reason = 'adulterado'"))
    with pytest.raises(Exception, match="config_versions"):
        asyncio.run(attempt("DELETE FROM config_versions"))

    command_row = (
        "INSERT INTO control_commands (id, name, operator, reason, state, created_at) "
        "VALUES ('{id}', 'pause', 'ana', 'x', '{state}', now())"
    )
    asyncio.run(attempt(command_row.format(id="a" * 32, state="pending_delivery")))
    with pytest.raises(Exception, match="ck_control_commands_state"):
        asyncio.run(attempt(command_row.format(id="b" * 32, state="sumiu")))
    job_row = (
        "INSERT INTO backtest_jobs (id, created_by, status, params, created_at) "
        "VALUES ('{id}', 'ana', '{status}', '{{}}', now())"
    )
    asyncio.run(attempt(job_row.format(id="a" * 32, status="queued")))
    with pytest.raises(Exception, match="ck_backtest_jobs_status"):
        asyncio.run(attempt(job_row.format(id="b" * 32, status="explodiu")))
