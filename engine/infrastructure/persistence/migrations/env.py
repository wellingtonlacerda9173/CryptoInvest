import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import URL, Connection
from sqlalchemy.ext.asyncio import create_async_engine

import api.persistence.models  # noqa: F401  (registra as tabelas da API no metadata)
from engine.infrastructure.config.settings import load_settings
from engine.infrastructure.persistence.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _database_url() -> URL | str:
    # Permite que testes injetem a URL via config.attributes sem tocar em segredos
    return config.attributes.get("database_url") or load_settings().database_url


def _run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def _run_async() -> None:
    engine = create_async_engine(_database_url())
    async with engine.connect() as connection:
        await connection.run_sync(_run_migrations)
    await engine.dispose()


def _run_offline() -> None:
    # `alembic upgrade|downgrade ... --sql`: renderiza o DDL sem conectar (sem banco, sem segredos)
    context.configure(
        url="postgresql+asyncpg://",
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    _run_offline()
else:
    asyncio.run(_run_async())
