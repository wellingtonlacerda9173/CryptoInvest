"""API das lições e do valor da IA: leitor lê, admin revisa (reauth + CSRF), tudo auditado."""

import asyncio
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

import api.services.ai_lessons as lessons_service
from engine.application.ai.ledger import AiCallRecord
from engine.application.ai.lesson_job import NewLesson
from engine.application.ai.lessons import build_summary, deterministic_report
from engine.infrastructure.persistence.ai_call_repository import SqlAiCallStore
from engine.infrastructure.persistence.lesson_repository import SqlLessonStore
from engine.infrastructure.persistence.models import AuditLogRow
from tests.integration.api.conftest import Harness, Session
from tests.integration.api.runner_support import (
    fake_link,
    module_db_url,
    module_sessions,
    rh,
    rh_admin,
    rh_reader,
)

pytestmark = pytest.mark.integration
__all__ = ["fake_link", "module_db_url", "module_sessions", "rh", "rh_admin", "rh_reader"]


async def seed(rh: Harness, minutes: int = 0) -> str:
    at = rh.clock() - timedelta(minutes=minutes)
    summary = build_summary([], {"ai_veto": 1}, {}, start=at - timedelta(days=7), end=at)
    return await SqlLessonStore(rh.sessions).add(
        NewLesson(
            created_at=at,
            period_start=summary.period_start,
            period_end=summary.period_end,
            source="deterministic",
            model=None,
            prompt_version="deterministic-v1",
            summary=summary,
            report=deterministic_report(summary),
        )
    )


async def review(
    session: Session, lesson_id: str, decision: str = "approve", reason: str = "faz sentido"
) -> httpx.Response:
    return await session.send(
        "POST", f"/ai/lessons/{lesson_id}/review", json={"decision": decision, "reason": reason}
    )


async def audit_actions(rh: Harness) -> list[str]:
    async with rh.sessions() as db:
        return [r.action for r in await db.scalars(select(AuditLogRow).order_by(AuditLogRow.id))]


async def test_reader_and_admin_read_lessons_with_pagination_and_detail(
    rh: Harness, rh_reader: Session, rh_admin: Session
) -> None:
    ids = [await seed(rh, minutes=m) for m in (3, 2, 1)]
    for session in (rh_reader, rh_admin):
        first = (await session.get("/ai/lessons", params={"limit": 2})).json()
        assert [i["id"] for i in first["items"]] == [ids[2], ids[1]] and first["next_cursor"]
        assert all(i["status"] == "pending" for i in first["items"])
        second = (
            await session.get("/ai/lessons", params={"limit": 2, "cursor": first["next_cursor"]})
        ).json()
        assert [i["id"] for i in second["items"]][:1] == [ids[0]]
        detail = (await session.get(f"/ai/lessons/{ids[0]}")).json()
        assert detail["observations"] and detail["stats"]["decisions"] == {"ai_veto": 1}
    assert (await rh_reader.get("/ai/lessons/" + "f" * 32)).status_code == 404
    assert (await rh_reader.get("/ai/lessons", params={"cursor": "lixo"})).status_code == 422


async def test_anonymous_cannot_read(rh: Harness) -> None:
    async with rh.client() as anonymous:
        for path in ("/ai/lessons", "/ai/value", "/ai/lessons/" + "a" * 32):
            assert (await anonymous.get(path)).status_code == 401


async def test_reader_cannot_review_or_run(rh: Harness, rh_reader: Session) -> None:
    lesson_id = await seed(rh)
    assert (await review(rh_reader, lesson_id)).status_code == 403
    assert (await rh_reader.send("POST", "/ai/lessons/run")).status_code == 403
    assert await SqlLessonStore(rh.sessions).approved(5) == []


async def test_review_needs_csrf_recent_reauth_and_a_reason(rh: Harness, rh_admin: Session) -> None:
    lesson_id = await seed(rh)
    no_csrf = await rh_admin.client.post(
        f"/ai/lessons/{lesson_id}/review", json={"decision": "approve", "reason": "sem csrf"}
    )
    assert no_csrf.status_code == 403
    assert (await review(rh_admin, lesson_id, reason="")).status_code == 422
    rh.clock.advance(minutes=6)
    stale = await review(rh_admin, lesson_id)
    assert stale.status_code == 403 and stale.json()["type"].endswith("fresh-auth-required")
    assert await SqlLessonStore(rh.sessions).approved(5) == []


async def test_admin_approves_then_rejects_and_everything_is_audited(rh: Harness) -> None:
    session = await rh.login(await rh.create_user("admin"))
    try:
        lesson_id = await seed(rh)
        approved = await review(session, lesson_id)
        body = approved.json()
        assert approved.status_code == 200 and body["status"] == "approved"
        assert body["reviewer"] == session.username and body["review_reason"] == "faz sentido"
        assert [lesson.id for lesson in await SqlLessonStore(rh.sessions).approved(5)] == [lesson_id]
        assert (await review(session, lesson_id)).status_code == 409
        rejected = await review(session, lesson_id, "reject", "dados insuficientes")
        assert rejected.json()["status"] == "rejected"
        assert await SqlLessonStore(rh.sessions).approved(5) == []
        assert (await session.get(f"/ai/lessons/{lesson_id}")).json()["status"] == "rejected"
        assert (await review(session, "e" * 32)).status_code == 404
        assert (await audit_actions(rh)).count("ai.lesson.reviewed") >= 2
    finally:
        await session.aclose()


class FakeProcess:
    def __init__(self) -> None:
        self.release = asyncio.Event()

    async def wait(self) -> int:
        await self.release.wait()
        return 0

    def kill(self) -> None:
        self.release.set()


async def test_run_is_admin_only_async_single_flight_and_audited(
    rh: Harness, rh_admin: Session, rh_reader: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[tuple[str, tuple[str, ...]]] = []
    process = FakeProcess()

    async def fake_spawn(timeframe: str, symbols: tuple[str, ...]) -> FakeProcess:
        started.append((timeframe, symbols))
        return process

    monkeypatch.setattr(lessons_service, "spawn_lessons", fake_spawn)
    assert (await rh_reader.send("POST", "/ai/lessons/run")).status_code == 403 and started == []
    first = await rh_admin.send("POST", "/ai/lessons/run")
    assert first.status_code == 202 and first.json()["state"] == "started" and len(started) == 1
    second = await rh_admin.send("POST", "/ai/lessons/run")
    assert second.status_code == 409 and len(started) == 1
    process.release.set()
    await asyncio.sleep(0.05)
    third = await rh_admin.send("POST", "/ai/lessons/run")
    assert third.status_code == 202 and len(started) == 2
    process.release.set()
    actions = await audit_actions(rh)
    assert actions.count("ai.lesson.run_requested") >= 2 and "ai.lesson.run_refused" in actions


async def test_value_report_is_honest_about_small_samples(rh: Harness, rh_reader: Session) -> None:
    empty = (await rh_reader.get("/ai/value")).json()
    assert empty["verdict"] in {"no_activity", "insufficient_sample"}
    await SqlAiCallStore(rh.sessions).record(
        AiCallRecord(
            called_at=rh.clock(),
            model="claude-haiku-5-5",
            prompt_version="analyst-v2",
            prompt_hash="c" * 64,
            cost_usd=Decimal("0.02"),
            result="ok",
        )
    )
    rh.container.gate_cache.clear()
    body = (await rh_reader.get("/ai/value", params={"days": 30})).json()
    assert body["calls"] >= 1 and body["cost_usd"] >= 0.02 and body["small_sample"] is True
    assert body["verdict"] == "insufficient_sample" and "insuficiente" in body["verdict_text"]
    assert (await rh_reader.get("/ai/value", params={"days": 0})).status_code == 422
