import socket
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import URL
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from engine.infrastructure.config.settings import Settings, load_settings
from engine.infrastructure.persistence.session import create_engine, create_session_factory
from tests.integration.disposable_db import (
    RoleSetup,
    assert_disposable_database,
    disposable_database,
    restricted_roles,
    upgrade_database,
)


def _reachable(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=3):
            return True
    except OSError:
        return False


@pytest.fixture(scope="session")
def settings() -> Settings:
    try:
        return load_settings()
    except Exception as exc:
        pytest.skip(f"settings indisponiveis (rode scripts/init-dev.ps1): {exc.__class__.__name__}")


@pytest.fixture(scope="session")
def db_ready(settings: Settings) -> None:
    if not _reachable(settings.database_host, settings.database_port):
        pytest.skip("Postgres/Timescale indisponivel (docker compose -f docker-compose.dev.yml up -d)")
    config = Config("alembic.ini")
    command.upgrade(config, "head")


@pytest_asyncio.fixture
async def engine(settings: Settings, db_ready: None) -> AsyncIterator[AsyncEngine]:
    eng = create_engine(settings.database_url)
    yield eng
    await eng.dispose()


@pytest.fixture
def sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(engine)


@pytest.fixture(scope="session")
def scratch_url(settings: Settings, db_ready: None) -> Iterator[URL]:
    """Banco descartavel migrado ate o head, compartilhado pela sessao (testes destrutivos e de papeis)."""
    with disposable_database(settings, "scratch") as url:
        upgrade_database(url)
        yield url


@pytest.fixture(scope="session")
def role_setup(settings: Settings, scratch_url: URL) -> Iterator[RoleSetup]:
    with restricted_roles(settings, scratch_url) as roles:
        yield roles


@pytest_asyncio.fixture
async def scratch_engine(scratch_url: URL) -> AsyncIterator[AsyncEngine]:
    assert_disposable_database(scratch_url)
    eng = create_engine(scratch_url)
    yield eng
    await eng.dispose()


@pytest.fixture
def scratch_sessions(scratch_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(scratch_engine)


@pytest_asyncio.fixture
async def app_engine(role_setup: RoleSetup) -> AsyncIterator[AsyncEngine]:
    """Conexao com o papel restrito do robo (`cryptoinvest_app`) no banco descartavel."""
    assert_disposable_database(role_setup.app_url)
    eng = create_engine(role_setup.app_url)
    yield eng
    await eng.dispose()


@pytest.fixture
def migration_url(settings: Settings, db_ready: None) -> Iterator[URL]:
    """Banco descartavel vazio e exclusivo do teste (downgrades/upgrades de migracao)."""
    with disposable_database(settings, "migtest") as url:
        yield url


@pytest.fixture(scope="session")
def binance_reachable() -> None:
    if not _reachable("api.binance.com", 443):
        pytest.skip("api.binance.com inacessivel")
