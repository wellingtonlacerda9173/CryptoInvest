"""Migracao 0016: indice (symbol, decided_at) em decisions, puramente aditivo."""

import asyncio

from alembic import command
from alembic.config import Config
from sqlalchemy import URL, text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration.disposable_db import assert_disposable_database

INDEX = "ix_decisions_symbol_decided_at"


def _indexes(url: URL) -> set[str]:
    async def go() -> set[str]:
        engine = create_async_engine(url)
        try:
            async with engine.begin() as conn:
                rows = await conn.execute(
                    text("SELECT indexname FROM pg_indexes WHERE tablename = 'decisions'")
                )
                return {r[0] for r in rows}
        finally:
            await engine.dispose()

    return asyncio.run(go())


def test_index_is_created_dropped_and_recreated_with_a_clean_check(migration_url: URL) -> None:
    assert_disposable_database(migration_url)
    config = Config("alembic.ini")
    config.attributes["database_url"] = migration_url
    command.upgrade(config, "head")
    command.check(config)
    assert INDEX in _indexes(migration_url)
    command.downgrade(config, "0015")
    assert INDEX not in _indexes(migration_url)
    command.upgrade(config, "head")
    assert INDEX in _indexes(migration_url)
