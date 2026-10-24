"""QA FINAL: consumidor REAL do runner (Redis real) com o papel MINIMO do robo (db_roles.sql) e provas de
que esse papel nao enxerga users/control_commands/backtest_jobs nem a API altera colunas proibidas."""

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from redis import asyncio as aioredis
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from engine.infrastructure.config.settings import Settings
from engine.infrastructure.persistence.session import create_engine, create_session_factory
from engine.runner.command_channel import SqlAuditSink, sign_command
from engine.runner.paper_control import Command, CommandName, CommandQueue
from engine.runner.paper_events import RedisKeys
from engine.runner.paper_robot import BotState
from tests.integration.disposable_db import (
    RoleSetup,
    assert_disposable_database,
    disposable_database,
    restricted_roles,
    upgrade_database,
)
from tests.integration.test_qa_final_redis import RealClock
from tests.integration.test_qa_redis_channel_real import KEY, URL, Rig
from tests.unit.runner.fakes import Harness

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def roles(settings: Settings, db_ready: None) -> Iterator[RoleSetup]:
    with disposable_database(settings, "qafinalroles") as url:
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


async def test_real_consumer_with_the_minimal_robot_role_discards_poison_audits_it_and_still_stops(
    app_engine: AsyncEngine, tmp_path: Path
) -> None:
    raw = aioredis.from_url(URL, decode_responses=False)
    try:
        await raw.ping()
    except Exception:
        pytest.skip("Redis indisponivel")
    keys = RedisKeys(f"qafinalroles:{uuid.uuid4().hex[:8]}")
    rig = Rig(await Harness(clock=RealClock()).start(), raw, keys, tmp_path)
    rig.h.robot.commands = CommandQueue(tmp_path)
    sessions = create_session_factory(app_engine)
    rig.h.robot.audit_sink = SqlAuditSink(sessions)
    try:
        now = rig.h.clock.now()
        old = sign_command(
            KEY, Command(name=CommandName.PAUSE, operator="ana", reason="t", issued_at=now),
            command_id="stale0000001", signed_at=now - timedelta(minutes=5),
        )  # fmt: skip
        stop_id, stop = rig.envelope(CommandName.EMERGENCY_STOP)
        await rig.push(
            b"\xff\xfe", old.encode(), b"lixo", json.dumps({"payload": {}, "sig": "x"}), stop.encode()
        )

        for _ in range(5):
            await rig.h.robot.housekeeping()

        assert rig.h.robot.bot_state is BotState.PAUSED
        assert (await rig.ack(stop_id) or {}).get("result") == "applied"
        async with app_engine.connect() as conn:
            codes = (
                (
                    await conn.execute(
                        text("SELECT payload->>'code' FROM audit_log WHERE action='paper_command_discarded'")
                    )
                )
                .scalars()
                .all()
            )
        assert set(codes) >= {"malformed", "stale"}, codes
    finally:
        async for key in raw.scan_iter(match=f"{keys.prefix}:*"):
            await raw.delete(key)
        await rig.source.close()
        await raw.aclose()


@pytest.mark.parametrize(
    "table",
    ["users", "user_sessions", "recovery_codes", "exchange_credentials", "control_commands", "backtest_jobs"],
)
async def test_robot_role_cannot_even_read_api_tables(app_engine: AsyncEngine, table: str) -> None:
    async with app_engine.connect() as conn:
        with pytest.raises(DBAPIError, match="permission denied"):
            await conn.execute(text(f"SELECT 1 FROM {table} LIMIT 1"))


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE users SET username = 'x'",
        "UPDATE users SET id = gen_random_uuid()",
        "UPDATE users SET created_at = now()",
        "DELETE FROM users",
        "TRUNCATE users",
        "UPDATE audit_log SET actor = 'x'",
        "DELETE FROM audit_log",
        "UPDATE control_commands SET name = 'x'",
        "UPDATE control_commands SET params = '{}'",
        "UPDATE backtest_jobs SET params = '{}'",
        "INSERT INTO trades (id) VALUES (1)",
        "UPDATE config_versions SET actor = 'x'",
        "DELETE FROM config_versions",
    ],
)
async def test_api_role_is_denied_the_forbidden_writes(api_engine: AsyncEngine, statement: str) -> None:
    async with api_engine.connect() as conn:
        with pytest.raises(DBAPIError, match=r"permission denied|must be owner|does not exist|violates"):
            await conn.execute(text(statement))


async def test_api_role_can_update_only_the_whitelisted_user_columns(api_engine: AsyncEngine) -> None:
    uid = uuid.uuid4()
    async with api_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, username, password_hash, role) VALUES (:i, 'ana.qa', 'h', 'reader')"
            ),
            {"i": uid},
        )
        for column, value in [
            ("failed_attempts", "2"),
            ("lock_epoch", "5"),
            ("disabled", "true"),
            ("role", "'admin'"),
        ]:
            await conn.execute(text(f"UPDATE users SET {column} = {value} WHERE id = :i"), {"i": uid})
