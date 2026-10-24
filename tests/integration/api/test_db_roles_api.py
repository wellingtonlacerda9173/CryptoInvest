"""Papeis do banco: robo (`cryptoinvest_app`) e API (`cryptoinvest_api`) separados (scripts/db_roles.sql)."""

from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from sqlalchemy import URL, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from api.app import create_app
from api.binance_keys import FakeKeyVerifier
from engine.infrastructure.config.settings import Settings
from engine.infrastructure.persistence.session import create_engine, create_session_factory
from tests.api_support import FakeClock, make_settings
from tests.integration.api.conftest import Harness, fake_key
from tests.integration.disposable_db import (
    RoleSetup,
    assert_disposable_database,
    disposable_database,
    restricted_roles,
    upgrade_database,
)

pytestmark = pytest.mark.integration

PRIVATE_TABLES = ("users", "user_sessions", "recovery_codes", "exchange_credentials")


@pytest.fixture(scope="module")
def roles(settings: Settings, db_ready: None) -> Iterator[RoleSetup]:
    with disposable_database(settings, "apiroles") as url:
        upgrade_database(url)
        with restricted_roles(settings, url) as setup:
            yield setup


@pytest_asyncio.fixture
async def app_engine(roles: RoleSetup) -> AsyncIterator[AsyncEngine]:
    assert_disposable_database(roles.app_url)
    engine = create_engine(roles.app_url)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def api_engine(roles: RoleSetup) -> AsyncIterator[AsyncEngine]:
    assert_disposable_database(roles.api_url)
    engine = create_engine(roles.api_url)
    yield engine
    await engine.dispose()


async def _denied(engine: AsyncEngine, statement: str) -> None:
    with pytest.raises(DBAPIError, match=r"permission denied|must be owner"):
        async with engine.begin() as conn:
            await conn.execute(text(statement))


@pytest.mark.parametrize("table", PRIVATE_TABLES)
@pytest.mark.parametrize("verb", ["SELECT * FROM {t}", "INSERT INTO {t} DEFAULT VALUES", "DELETE FROM {t}"])
async def test_robot_role_cannot_touch_user_session_or_credential_tables(
    app_engine: AsyncEngine, table: str, verb: str
) -> None:
    await _denied(app_engine, verb.format(t=table))


async def test_robot_role_keeps_its_journal_and_config_read_access(app_engine: AsyncEngine) -> None:
    async with app_engine.begin() as conn:
        await conn.execute(text("SELECT count(*) FROM trades"))
        await conn.execute(text("SELECT count(*) FROM config_versions"))


@pytest.mark.parametrize("table", ["control_commands", "backtest_jobs"])
@pytest.mark.parametrize("verb", ["SELECT * FROM {t}", "INSERT INTO {t} DEFAULT VALUES", "DELETE FROM {t}"])
async def test_robot_role_has_no_access_to_tables_it_does_not_use(
    app_engine: AsyncEngine, table: str, verb: str
) -> None:
    await _denied(app_engine, verb.format(t=table))


async def test_robot_role_cannot_write_config_versions(app_engine: AsyncEngine) -> None:
    await _denied(app_engine, "INSERT INTO config_versions DEFAULT VALUES")


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE control_commands SET name = name",
        "UPDATE control_commands SET reason = reason",
        "UPDATE control_commands SET params = params",
        "UPDATE control_commands SET operator = operator",
        "UPDATE control_commands SET created_at = created_at",
        "UPDATE backtest_jobs SET params = params",
        "UPDATE backtest_jobs SET created_by = created_by",
    ],
)
async def test_api_role_cannot_rewrite_what_a_command_or_job_asked_for(
    api_engine: AsyncEngine, statement: str
) -> None:
    await _denied(api_engine, statement)


async def test_api_role_can_advance_command_and_job_state(api_engine: AsyncEngine) -> None:
    async with api_engine.begin() as conn:
        await conn.execute(text("UPDATE control_commands SET state = state, result = result WHERE false"))
        await conn.execute(text("SELECT id FROM control_commands LIMIT 1 FOR UPDATE"))
        await conn.execute(text("UPDATE backtest_jobs SET status = status, progress = progress WHERE false"))


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE users SET username = username WHERE false",
        "UPDATE users SET id = id WHERE false",
        "UPDATE users SET created_at = created_at WHERE false",
        "DELETE FROM users",
        "TRUNCATE users",
        "UPDATE audit_log SET action = action",
        "DELETE FROM audit_log",
        "TRUNCATE audit_log",
        "UPDATE config_versions SET actor = actor",
        "DELETE FROM config_versions",
        "INSERT INTO trades DEFAULT VALUES",
        "UPDATE trades SET run_id = run_id",
        "INSERT INTO runs DEFAULT VALUES",
        "DELETE FROM control_commands",
        "CREATE TABLE intruso (id int)",
        "ALTER TABLE users ADD COLUMN x int",
        "DROP TABLE users",
        "SELECT * FROM alembic_version",
    ],
)
async def test_api_role_is_limited_to_what_it_needs(api_engine: AsyncEngine, statement: str) -> None:
    await _denied(api_engine, statement)


async def test_api_role_can_do_its_normal_work(api_engine: AsyncEngine) -> None:
    async with api_engine.begin() as conn:
        await conn.execute(text("SELECT count(*) FROM users"))
        await conn.execute(text("SELECT count(*) FROM trades"))
        await conn.execute(text("UPDATE users SET role = role, disabled = disabled WHERE false"))
        await conn.execute(text("DELETE FROM user_sessions WHERE false"))
        await conn.execute(text("DELETE FROM recovery_codes WHERE false"))
        await conn.execute(text("DELETE FROM exchange_credentials WHERE false"))
        await conn.execute(
            text("INSERT INTO audit_log (actor, action, entity, payload) VALUES ('t', 't', 't', '{}')")
        )


async def test_whole_api_flow_runs_under_the_restricted_api_role(roles: RoleSetup) -> None:
    engine = create_engine(roles.api_url)
    clock = FakeClock()
    verifier = FakeKeyVerifier()
    sessions = create_session_factory(engine)
    app = create_app(make_settings(), sessions=sessions, verifier=verifier, clock=clock)
    harness = Harness(
        app=app, container=app.state.container, clock=clock, verifier=verifier, sessions=sessions
    )
    try:
        admin = await harness.login(await harness.create_user("admin"))
        key, secret = fake_key()
        created = await admin.send(
            "POST", "/credentials", json={"label": "Conta", "api_key": key, "api_secret": secret}
        )
        assert created.status_code == 201, created.text
        other = await admin.send(
            "POST",
            "/users",
            json={"username": "leitor.papeis", "password": "bule roxo canta tres musicas 42?"},
        )
        assert other.status_code == 201, other.text
        user_id = other.json()["id"]
        assert (await admin.send("PATCH", f"/users/{user_id}", json={"disabled": True})).status_code == 200
        assert (await admin.send("POST", f"/users/{user_id}/unlock")).status_code == 200
        reset = await admin.send("POST", f"/users/{user_id}/reset-two-factor")
        assert reset.status_code == 200
        assert (await admin.get("/audit")).status_code == 200
        assert (await admin.send("DELETE", f"/credentials/{created.json()['id']}")).status_code == 204
        assert (await admin.send("DELETE", "/sessions")).status_code == 204
        assert (await admin.send("POST", "/auth/logout")).status_code == 204
        await admin.aclose()
    finally:
        await engine.dispose()


async def test_check_grants_passes_for_the_api_role_and_flags_the_robot_role(
    api_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession

    from api.cli import REQUIRED_API_UPDATES, missing_api_grants

    async with AsyncSession(api_engine) as session:
        assert await missing_api_grants(session) == []
    async with AsyncSession(app_engine) as session:
        missing = await missing_api_grants(session)
    assert "users.lock_epoch" in missing
    assert len(missing) == sum(len(columns) for columns in REQUIRED_API_UPDATES.values())
