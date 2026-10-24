import asyncio

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import URL, text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration.disposable_db import assert_disposable_database

pytestmark = pytest.mark.integration

API_TABLES = {"users", "user_sessions", "recovery_codes", "exchange_credentials"}


def _config(url: URL) -> Config:
    assert_disposable_database(url)
    config = Config("alembic.ini")
    config.attributes["database_url"] = url
    return config


async def _tables(url: URL) -> set[str]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            rows = await conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))
            return {r[0] for r in rows}
    finally:
        await engine.dispose()


def test_upgrade_downgrade_upgrade_and_check_is_clean(migration_url: URL) -> None:
    config = _config(migration_url)
    command.upgrade(config, "head")
    assert asyncio.run(_tables(migration_url)) >= API_TABLES
    command.check(config)

    command.downgrade(config, "0005")
    remaining = asyncio.run(_tables(migration_url))
    assert not API_TABLES & remaining and {"audit_log", "trades"} <= remaining

    command.upgrade(config, "head")
    assert asyncio.run(_tables(migration_url)) >= API_TABLES
    command.check(config)


def test_constraints_reject_invalid_rows(migration_url: URL) -> None:
    config = _config(migration_url)
    command.upgrade(config, "head")

    async def attempt(sql: str) -> None:
        engine = create_async_engine(migration_url)
        try:
            async with engine.begin() as conn:
                await conn.execute(text(sql))
        finally:
            await engine.dispose()

    base = (
        "INSERT INTO users (id, username, password_hash, role) "
        "VALUES (gen_random_uuid(), '{name}', 'h', '{role}')"
    )
    asyncio.run(attempt(base.format(name="ok.user", role="admin")))
    for bad in (
        base.format(name="ok.user", role="admin"),
        base.format(name="x.y.z", role="root"),
        base.format(name="Upper", role="admin"),
    ):
        with pytest.raises(Exception, match="users"):
            asyncio.run(attempt(bad))
