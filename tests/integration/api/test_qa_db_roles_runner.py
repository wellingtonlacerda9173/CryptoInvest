"""QA 2a rodada: o consumidor de comandos do runner sob o papel de banco MINIMO do robo (db_roles.sql)."""

from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from engine.infrastructure.config.settings import Settings
from engine.infrastructure.persistence.config_repository import ConfigRepository
from engine.infrastructure.persistence.session import create_engine, create_session_factory
from engine.runner.command_channel import SqlAuditSink
from tests.integration.disposable_db import (
    RoleSetup,
    assert_disposable_database,
    disposable_database,
    restricted_roles,
    upgrade_database,
)

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def roles(settings: Settings, db_ready: None) -> Iterator[RoleSetup]:
    with disposable_database(settings, "qaroles") as url:
        upgrade_database(url)
        with restricted_roles(settings, url) as setup:
            yield setup


@pytest_asyncio.fixture
async def app_engine(roles: RoleSetup) -> AsyncIterator[AsyncEngine]:
    assert_disposable_database(roles.app_url)
    engine = create_engine(roles.app_url)
    yield engine
    await engine.dispose()


async def test_the_runner_audit_sink_and_config_read_work_with_the_minimal_robot_role(
    app_engine: AsyncEngine,
) -> None:
    sessions = create_session_factory(app_engine)
    await SqlAuditSink(sessions).audit("system", "paper_command_discarded", "run-x", {"code": "stale"})
    await SqlAuditSink(sessions).audit("system", "config_applied", "run-x", {"fields": ["max_drawdown"]})

    async with sessions() as db:
        assert await ConfigRepository(db).latest() is None
    async with app_engine.connect() as conn:
        rows = (await conn.execute(text("SELECT action FROM audit_log ORDER BY id"))).scalars().all()
    assert rows == ["paper_command_discarded", "config_applied"]
