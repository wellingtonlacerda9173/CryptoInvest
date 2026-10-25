"""Migracao 0013, tabelas append-only, papeis e repositorio de licoes, em bancos descartaveis `*_test`."""

import asyncio
import socket
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import URL, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from engine.application.ai.lesson_job import NewLesson
from engine.application.ai.lessons import build_summary, deterministic_report
from engine.infrastructure.config.settings import Settings, load_settings
from engine.infrastructure.persistence.lesson_repository import LessonRepository, SqlLessonStore
from engine.infrastructure.persistence.session import create_engine, create_session_factory
from engine.runner.lessons import generate_lesson
from tests.integration.disposable_db import (
    RoleSetup,
    assert_disposable_database,
    disposable_database,
    restricted_roles,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def db_settings() -> Settings:
    try:
        loaded = load_settings()
    except Exception as exc:
        pytest.skip(f"settings indisponiveis: {exc.__class__.__name__}")
    try:
        with socket.create_connection((loaded.database_host, loaded.database_port), timeout=3):
            pass
    except OSError:
        pytest.skip("Postgres/Timescale indisponivel (docker compose -f docker-compose.dev.yml up -d)")
    return loaded


def alembic_config(url: URL) -> Config:
    assert_disposable_database(url)
    config_ = Config("alembic.ini")
    config_.attributes["database_url"] = url
    return config_


@pytest.fixture
def head_url(db_settings: Settings) -> Iterator[URL]:
    with disposable_database(db_settings, "lessons") as url:
        command.upgrade(alembic_config(url), "head")
        yield url


@pytest_asyncio.fixture
async def engine(head_url: URL) -> AsyncIterator[AsyncEngine]:
    eng = create_engine(head_url)
    yield eng
    await eng.dispose()


@pytest.fixture
def sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(engine)


def new_lesson(at: datetime = NOW) -> NewLesson:
    summary = build_summary([], {"ai_veto": 2}, {}, start=at - timedelta(days=7), end=at)
    return NewLesson(
        created_at=at,
        period_start=summary.period_start,
        period_end=summary.period_end,
        source="deterministic",
        model=None,
        prompt_version="deterministic-v1",
        summary=summary,
        report=deterministic_report(summary),
    )


def test_migration_0013_up_down_up_and_alembic_check(db_settings: Settings) -> None:
    async def exists(url: URL) -> list[bool]:
        eng = create_engine(url)
        try:
            async with eng.connect() as conn:
                return [
                    await conn.scalar(text(f"SELECT to_regclass('public.{t}')")) is not None
                    for t in ("ai_lessons", "ai_lesson_reviews")
                ]
        finally:
            await eng.dispose()

    with disposable_database(db_settings, "mig13") as url:
        cfg = alembic_config(url)
        command.upgrade(cfg, "0012")
        assert asyncio.run(exists(url)) == [False, False]
        command.upgrade(cfg, "head")
        assert asyncio.run(exists(url)) == [True, True]
        command.check(cfg)
        command.downgrade(cfg, "0012")
        assert asyncio.run(exists(url)) == [False, False]
        command.upgrade(cfg, "head")
        command.check(cfg)
        assert asyncio.run(exists(url)) == [True, True]


async def test_review_state_lives_in_the_child_table_and_the_latest_review_wins(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    lesson_id = await SqlLessonStore(sessions).add(new_lesson())
    store = SqlLessonStore(sessions)
    assert await store.approved(5) == []
    async with sessions() as session:
        assert (await LessonRepository(session).get(lesson_id)).status == "pending"  # type: ignore[union-attr]
    async with sessions.begin() as session:
        await LessonRepository(session).add_review(
            lesson_id, status="approved", reviewer="ana", reason="faz sentido", at=NOW
        )
    [approved] = await store.approved(5)
    assert approved.id == lesson_id and approved.status == "approved" and approved.reviewer == "ana"
    async with sessions.begin() as session:
        await LessonRepository(session).add_review(
            lesson_id, status="rejected", reviewer="ana", reason="mudei de ideia", at=NOW + timedelta(hours=1)
        )
    assert await store.approved(5) == []
    async with sessions() as session:
        stored = await LessonRepository(session).get(lesson_id)
    assert stored is not None and stored.status == "rejected" and stored.review_reason == "mudei de ideia"


async def test_tables_are_append_only_and_reviews_require_admin_and_reason(
    sessions: async_sessionmaker[AsyncSession], engine: AsyncEngine
) -> None:
    lesson_id = await SqlLessonStore(sessions).add(new_lesson())
    async with sessions.begin() as session:
        await LessonRepository(session).add_review(
            lesson_id, status="approved", reviewer="ana", reason="ok mesmo", at=NOW
        )
    for statement in (
        "UPDATE ai_lessons SET source = 'ai'",
        "DELETE FROM ai_lessons",
        "TRUNCATE ai_lessons CASCADE",
        "UPDATE ai_lesson_reviews SET status = 'rejected'",
        "DELETE FROM ai_lesson_reviews",
        "TRUNCATE ai_lesson_reviews",
    ):
        with pytest.raises(DBAPIError, match=r"append-only|forbid|nao"):
            async with engine.begin() as conn:
                await conn.execute(text(statement))
    insert = (
        "INSERT INTO ai_lesson_reviews (lesson_id, status, reviewer, reviewer_role, reason, reviewed_at) "
        "VALUES (:l, :s, 'x', :r, :why, now())"
    )
    for params, check in (
        ({"s": "approved", "r": "reader", "why": "motivo"}, "ck_ai_lesson_reviews_admin"),
        ({"s": "approved", "r": "admin", "why": "  "}, "ck_ai_lesson_reviews_reason"),
        ({"s": "talvez", "r": "admin", "why": "motivo"}, "ck_ai_lesson_reviews_status"),
    ):
        with pytest.raises(IntegrityError, match=check):
            async with engine.begin() as conn:
                await conn.execute(text(insert), {"l": lesson_id, **params})


def test_roles_robot_writes_lessons_api_writes_reviews_nobody_updates(db_settings: Settings) -> None:
    with disposable_database(db_settings, "lessonroles") as url:
        command.upgrade(alembic_config(url), "head")
        with restricted_roles(db_settings, url) as roles:
            asyncio.run(check_roles(roles))


async def check_roles(roles: RoleSetup) -> None:
    app, api = create_engine(roles.app_url), create_engine(roles.api_url)
    try:
        robot = SqlLessonStore(create_session_factory(app))
        lesson_id = await robot.add(new_lesson())
        assert await robot.approved(5) == []
        review = (
            "INSERT INTO ai_lesson_reviews (lesson_id, status, reviewer, reviewer_role, reason, reviewed_at) "
            "VALUES (:l, 'approved', 'ana', 'admin', 'motivo', now())"
        )
        with pytest.raises(DBAPIError, match="permission denied"):
            async with app.begin() as conn:
                await conn.execute(text(review), {"l": lesson_id})
        async with api.begin() as conn:
            await conn.execute(text(review), {"l": lesson_id})
        assert [lesson.id for lesson in await robot.approved(5)] == [lesson_id]
        lesson_insert = (
            "INSERT INTO ai_lessons (id, created_at, period_start, period_end, source, prompt_version, "
            "summary, report) VALUES ('" + "b" * 32 + "', now(), now(), now(), 'ai', 'v', '{}', '{}')"
        )
        with pytest.raises(DBAPIError, match="permission denied"):
            async with api.begin() as conn:
                await conn.execute(text(lesson_insert))
        for engine_, statements in (
            (
                app,
                (
                    "UPDATE ai_lessons SET source = 'ai'",
                    "DELETE FROM ai_lessons",
                    "ALTER TABLE ai_lessons DISABLE TRIGGER ai_lessons_append_only",
                ),
            ),
            (
                api,
                (
                    "UPDATE ai_lesson_reviews SET status = 'rejected'",
                    "DELETE FROM ai_lesson_reviews",
                    "UPDATE ai_lessons SET source = 'ai'",
                    "TRUNCATE ai_lesson_reviews",
                ),
            ),
        ):
            for statement in statements:
                with pytest.raises(DBAPIError, match=r"permission denied|must be owner"):
                    async with engine_.begin() as conn:
                        await conn.execute(text(statement))
    finally:
        await app.dispose()
        await api.dispose()


async def test_on_demand_job_without_ai_saves_a_pending_deterministic_lesson(
    sessions: async_sessionmaker[AsyncSession], db_settings: Settings, tmp_path: Path
) -> None:
    result = await generate_lesson(
        sessions, db_settings, timeframe="1h", samples_dir=tmp_path, use_ai=False, now=NOW
    )
    assert result.source == "deterministic" and result.trades == 0 and result.snapshot_id is None
    async with sessions() as session:
        [lesson] = await LessonRepository(session).page(10)
    assert lesson.id == result.lesson_id and lesson.status == "pending"
    assert "Sem operações" in lesson.report.summary
