import socket
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from engine.infrastructure.config.settings import Settings, load_settings
from engine.infrastructure.persistence.session import create_engine, create_session_factory


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
def binance_reachable() -> None:
    if not _reachable("api.binance.com", 443):
        pytest.skip("api.binance.com inacessivel")
