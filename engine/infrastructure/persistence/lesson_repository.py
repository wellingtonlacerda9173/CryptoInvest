import uuid
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from engine.application.ai.lesson_job import NewLesson
from engine.application.ai.lessons import LessonReport, StoredLesson
from engine.infrastructure.persistence.models import AiLessonReviewRow, AiLessonRow, DecisionRow
from engine.infrastructure.persistence.serialization import require_utc

ReviewDecision = Literal["approved", "rejected"]


def _stored(row: AiLessonRow, review: AiLessonReviewRow | None) -> StoredLesson:
    return StoredLesson(
        id=row.id,
        created_at=row.created_at,
        period_start=row.period_start,
        period_end=row.period_end,
        source=row.source,
        model=row.model,
        prompt_version=row.prompt_version,
        summary=row.summary,
        report=LessonReport.model_validate(row.report),
        status="pending" if review is None else review.status,
        reviewer=None if review is None else review.reviewer,
        review_reason=None if review is None else review.reason,
        reviewed_at=None if review is None else review.reviewed_at,
    )


class LessonRepository:
    """Licoes e revisoes sao append-only: so `add`, `add_review` e leituras (o banco recusa o resto)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _select(self) -> Any:
        newest = (
            select(AiLessonReviewRow.lesson_id, func.max(AiLessonReviewRow.id).label("review_id"))
            .group_by(AiLessonReviewRow.lesson_id)
            .subquery()
        )
        return (
            select(AiLessonRow, AiLessonReviewRow)
            .outerjoin(newest, newest.c.lesson_id == AiLessonRow.id)
            .outerjoin(AiLessonReviewRow, AiLessonReviewRow.id == newest.c.review_id)
        )

    async def add(self, lesson: NewLesson) -> str:
        lesson_id = uuid.uuid4().hex
        self._session.add(
            AiLessonRow(
                id=lesson_id,
                created_at=require_utc(lesson.created_at, "created_at"),
                period_start=require_utc(lesson.period_start, "period_start"),
                period_end=require_utc(lesson.period_end, "period_end"),
                source=lesson.source,
                model=lesson.model,
                prompt_version=lesson.prompt_version,
                summary=lesson.summary.model_dump(mode="json"),
                report=lesson.report.model_dump(mode="json"),
            )
        )
        await self._session.flush()
        return lesson_id

    async def get(self, lesson_id: str) -> StoredLesson | None:
        row = (await self._session.execute(self._select().where(AiLessonRow.id == lesson_id))).first()
        return None if row is None else _stored(row[0], row[1])

    async def page(self, limit: int, after: tuple[datetime, str] | None = None) -> list[StoredLesson]:
        stmt = self._select().order_by(AiLessonRow.created_at.desc(), AiLessonRow.id.desc()).limit(limit)
        if after is not None:
            stmt = stmt.where(tuple_(AiLessonRow.created_at, AiLessonRow.id) < after)
        return [_stored(r[0], r[1]) for r in (await self._session.execute(stmt)).all()]

    async def approved(self, limit: int) -> list[StoredLesson]:
        stmt = (
            self._select()
            .where(AiLessonReviewRow.status == "approved", AiLessonReviewRow.reviewer_role == "admin")
            .order_by(AiLessonRow.created_at.desc(), AiLessonRow.id.desc())
            .limit(limit)
        )
        return [_stored(r[0], r[1]) for r in (await self._session.execute(stmt)).all()]

    async def add_review(
        self, lesson_id: str, *, status: ReviewDecision, reviewer: str, reason: str, at: datetime
    ) -> None:
        self._session.add(
            AiLessonReviewRow(
                lesson_id=lesson_id,
                status=status,
                reviewer=reviewer,
                reviewer_role="admin",
                reason=reason.strip(),
                reviewed_at=require_utc(at, "at"),
            )
        )
        await self._session.flush()

    async def latest_created_at(self) -> datetime | None:
        return await self._session.scalar(select(func.max(AiLessonRow.created_at)))

    async def skipped_counts(self, start: datetime, end: datetime) -> dict[str, int]:
        """Sinais nao operados por motivo (so codigo e contagem: nada de precos, saldo ou run)."""
        stmt = (
            select(DecisionRow.code, func.count())
            .where(
                DecisionRow.traded.is_(False), DecisionRow.decided_at >= start, DecisionRow.decided_at <= end
            )
            .group_by(DecisionRow.code)
        )
        return {str(code): int(count) for code, count in (await self._session.execute(stmt)).all()}


class SqlLessonStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def add(self, lesson: NewLesson) -> str:
        async with self._sessions() as session, session.begin():
            return await LessonRepository(session).add(lesson)

    async def approved(self, limit: int) -> list[StoredLesson]:
        async with self._sessions() as session:
            return await LessonRepository(session).approved(limit)

    async def latest_created_at(self) -> datetime | None:
        async with self._sessions() as session:
            return await LessonRepository(session).latest_created_at()

    async def skipped_counts(self, start: datetime, end: datetime) -> dict[str, int]:
        async with self._sessions() as session:
            return await LessonRepository(session).skipped_counts(start, end)
