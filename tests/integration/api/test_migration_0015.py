"""Migracao 0015: autorizacoes do Live (append-only), revogacoes, frase por CHECK e grants dos papeis."""

import asyncio
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import URL, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from engine.infrastructure.config.settings import Settings
from engine.infrastructure.persistence.session import create_engine
from tests.integration.disposable_db import (
    RoleSetup,
    assert_disposable_database,
    disposable_database,
    restricted_roles,
    upgrade_database,
)

pytestmark = pytest.mark.integration

RUN = "INSERT INTO runs (run_id, mode, started_at, config_hash, code_version) VALUES ('r1', 'paper', now(), 'h', 'c')"
AUTH = (
    "INSERT INTO live_authorizations (id, paper_run_id, criteria, actor, reason, confirmed_phrase, "
    "initial_capital_quote, credential_id, key_permissions, record_hash, created_at, expires_at) "
    "VALUES ('{id}', 'r1', '{{}}', 'ana', '{reason}', '{phrase}', {capital}, 'c', '{{}}', 'h', now(), "
    "now() + interval '{ttl}')"
)
REVOCATION = (
    "INSERT INTO live_authorization_revocations (authorization_id, actor, reason, revoked_at) "
    "VALUES ('{id}', 'ana', 'motivo', now())"
)
PHRASE = "OPERAR COM DINHEIRO REAL"


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


def _good(id_: str = "a1", **overrides: str) -> str:
    values = {"id": id_, "reason": "motivo", "phrase": PHRASE, "capital": "50", "ttl": "1 day"} | overrides
    return AUTH.format(**values)


def test_up_down_up_and_alembic_check_is_clean(migration_url: URL) -> None:
    config = _config(migration_url)
    command.upgrade(config, "head")
    command.check(config)
    command.downgrade(config, "0014")
    tables = {
        r[0] for r in _run(migration_url, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    }
    assert not tables & {"live_authorizations", "live_authorization_revocations"}
    command.upgrade(config, "head")
    command.check(config)


def test_append_only_triggers_and_check_constraints(migration_url: URL) -> None:
    command.upgrade(_config(migration_url), "head")
    _run(migration_url, RUN)
    _run(migration_url, _good())
    _run(migration_url, REVOCATION.format(id="a1"))

    for sql in (
        "UPDATE live_authorizations SET reason = 'xx adulterado'",
        "DELETE FROM live_authorizations",
        "TRUNCATE live_authorizations",
        "UPDATE live_authorization_revocations SET reason = 'adulterado'",
        "DELETE FROM live_authorization_revocations",
        "TRUNCATE live_authorization_revocations",
    ):
        with pytest.raises(Exception, match=r"append|forbid|imut|modif|cannot truncate"):
            _run(migration_url, sql)
    with pytest.raises(Exception, match="uq_live_authorization_revocations_authorization"):
        _run(migration_url, REVOCATION.format(id="a1"))
    with pytest.raises(Exception, match="live_authorization_revocations_authorization_id_fkey"):
        _run(migration_url, REVOCATION.format(id="nao-existe"))
    for sql, constraint in (
        (_good("b1", phrase="operar com dinheiro real"), "ck_live_authorizations_phrase"),
        (_good("b2", phrase=PHRASE + " "), "ck_live_authorizations_phrase"),
        (_good("b3", capital="0"), "ck_live_authorizations_capital"),
        (_good("b4", ttl="-1 day"), "ck_live_authorizations_expiry"),
        (_good("b5", reason="  "), "ck_live_authorizations_reason"),
    ):
        with pytest.raises(Exception, match=constraint):
            _run(migration_url, sql)


@pytest.fixture(scope="module")
def roles(settings: Settings, db_ready: None) -> Iterator[RoleSetup]:
    with disposable_database(settings, "live15") as url:
        upgrade_database(url)
        _run(url, RUN)
        with restricted_roles(settings, url) as setup:
            yield setup


@pytest_asyncio.fixture
async def api_engine(roles: RoleSetup) -> AsyncIterator[AsyncEngine]:
    assert_disposable_database(roles.api_url)
    engine = create_engine(roles.api_url)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def app_engine(roles: RoleSetup) -> AsyncIterator[AsyncEngine]:
    assert_disposable_database(roles.app_url)
    engine = create_engine(roles.app_url)
    yield engine
    await engine.dispose()


async def test_api_role_inserts_and_reads_but_never_changes_or_deletes(
    api_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    async with api_engine.begin() as conn:
        await conn.execute(text(_good("g1")))
        await conn.execute(text(REVOCATION.format(id="g1")))
        assert (await conn.execute(text("SELECT count(*) FROM live_authorizations"))).scalar_one() == 1
    for statement in (
        "UPDATE live_authorizations SET reason = 'adulterado'",
        "DELETE FROM live_authorizations",
        "TRUNCATE live_authorizations",
        "UPDATE live_authorization_revocations SET reason = 'adulterado'",
        "DELETE FROM live_authorization_revocations",
    ):
        with pytest.raises(DBAPIError, match=r"permission denied|must be owner"):
            async with api_engine.begin() as conn:
                await conn.execute(text(statement))
    async with app_engine.begin() as conn:
        assert (await conn.execute(text("SELECT count(*) FROM live_authorizations"))).scalar_one() == 1
        assert (
            await conn.execute(text("SELECT count(*) FROM live_authorization_revocations"))
        ).scalar_one() == 1


async def test_robot_role_can_only_read_authorizations(app_engine: AsyncEngine) -> None:
    for statement in (
        _good("r-forged"),
        REVOCATION.format(id="g1"),
        "UPDATE live_authorizations SET reason = 'x'",
        "DELETE FROM live_authorizations",
        "DELETE FROM live_authorization_revocations",
    ):
        with pytest.raises(DBAPIError, match=r"permission denied|must be owner"):
            async with app_engine.begin() as conn:
                await conn.execute(text(statement))
