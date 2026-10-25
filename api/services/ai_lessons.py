"""Lições da IA e valor da IA. Leitor lê; só admin (com reautenticação, na rota) revisa e dispara a geração.

A geração roda em SUBPROCESSO (`python -m engine.runner.lessons`), fora do event loop e sem bloquear a API;
quem grava a lição é o papel do robô. A API só grava a revisão humana (append-only).
"""

import asyncio
import contextlib
import sys
import time
from pathlib import Path
from typing import Literal

import structlog

from api.audit import AuditAction, AuditResult, AuditService, RequestMeta
from api.container import Container
from api.cursors import decode_cursor, encode_cursor
from api.errors import Conflict, NotFound
from api.schemas_ai import (
    AiValueOut,
    LessonDetailOut,
    LessonOut,
    LessonPage,
    LessonRunOut,
    LessonStatsOut,
    ObservationOut,
    VetoCaseOut,
)
from engine.application.ai.lessons import StoredLesson
from engine.application.ai.value import MIN_RESOLVED
from engine.infrastructure.config.logging import scrub_text
from engine.infrastructure.persistence.config_repository import ConfigRepository
from engine.infrastructure.persistence.lesson_repository import LessonRepository
from engine.runner.ai_value import MAX_DAYS, measure_ai_value

log = structlog.get_logger()

ROOT = Path(__file__).resolve().parents[2]
RUN_TIMEOUT_SECONDS = 600.0
_RUN_KEY = "ai_lessons:run"
_VALUE_TTL_SECONDS = 60.0


def lesson_out(lesson: StoredLesson) -> LessonOut:
    return LessonOut(
        id=lesson.id,
        created_at=lesson.created_at,
        period_start=lesson.period_start,
        period_end=lesson.period_end,
        source=lesson.source,
        model=lesson.model,
        prompt_version=lesson.prompt_version,
        status=lesson.status,
        reviewer=lesson.reviewer,
        review_reason=lesson.review_reason,
        reviewed_at=lesson.reviewed_at,
        summary=lesson.report.summary,
        observations=[
            ObservationOut(text=o.text, evidence=o.evidence, confidence=o.confidence)
            for o in lesson.report.observations
        ],
    )


def detail_out(lesson: StoredLesson) -> LessonDetailOut:
    raw = lesson.summary
    return LessonDetailOut(
        **lesson_out(lesson).model_dump(),
        stats=LessonStatsOut.model_validate(
            {
                **{
                    k: raw[k]
                    for k in ("trades", "wins", "losses", "total_r", "loss_r", "skipped", "decisions")
                },
                **{k: raw[k] for k in ("by_strategy", "by_regime", "by_symbol", "worst_losses")},
            },
        ),
    )


async def spawn_lessons(timeframe: str, symbols: tuple[str, ...]) -> asyncio.subprocess.Process:
    command = [sys.executable, "-m", "engine.runner.lessons", "--timeframe", timeframe]
    if symbols:
        command += ["--symbols", ",".join(symbols)]
    return await asyncio.create_subprocess_exec(
        *command,
        cwd=ROOT,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )


async def _supervise(process: asyncio.subprocess.Process) -> None:
    try:
        await asyncio.wait_for(process.wait(), RUN_TIMEOUT_SECONDS)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
    except Exception as error:
        log.warning("lessons_run_failed", error=scrub_text(repr(error)))


class AiLessonService:
    def __init__(self, container: Container, audit: AuditService | None = None) -> None:
        self._c = container
        self._audit = audit

    async def list(self, *, limit: int, cursor: str | None) -> LessonPage:
        after = None
        if cursor is not None:
            decoded = decode_cursor(cursor)
            after = (decoded.moment, decoded.last_id)
        async with self._c.sessions() as db:
            rows = await LessonRepository(db).page(limit + 1, after)
        page = rows[:limit]
        more = len(rows) > limit
        return LessonPage(
            items=[lesson_out(r) for r in page],
            next_cursor=encode_cursor(page[-1].created_at, page[-1].id) if more and page else None,
        )

    async def get(self, lesson_id: str) -> StoredLesson:
        async with self._c.sessions() as db:
            lesson = await LessonRepository(db).get(lesson_id)
        if lesson is None:
            raise NotFound("Lição não encontrada.")
        return lesson

    async def detail(self, lesson_id: str) -> LessonDetailOut:
        return detail_out(await self.get(lesson_id))

    async def review(
        self, lesson_id: str, *, decision: str, reason: str, actor: str, meta: RequestMeta
    ) -> LessonDetailOut:
        assert self._audit is not None  # noqa: S101
        status: Literal["approved", "rejected"] = "approved" if decision == "approve" else "rejected"
        async with self._c.sessions.begin() as db:
            repo = LessonRepository(db)
            lesson = await repo.get(lesson_id)
            if lesson is None:
                raise NotFound("Lição não encontrada.")
            if lesson.status == status:
                raise Conflict("A lição já está nesse estado. Recarregue a página.")
            await repo.add_review(lesson_id, status=status, reviewer=actor, reason=reason, at=self._c.clock())
            await self._audit.record(
                actor=actor,
                action=AuditAction.AI_LESSON_REVIEWED,
                target=f"ai_lesson:{lesson_id}",
                result=AuditResult.SUCCESS,
                meta=meta,
                details={"decision": status, "previous": lesson.status, "reason": reason},
                session=db,
            )
        return await self.detail(lesson_id)

    async def run(self, *, actor: str, meta: RequestMeta) -> LessonRunOut:
        assert self._audit is not None  # noqa: S101
        running = self._c.gate_cache.get(_RUN_KEY)
        if running is not None and not running[1].done():
            await self._audit.record(
                actor=actor,
                action=AuditAction.AI_LESSON_RUN_REFUSED,
                target="ai_lessons",
                result=AuditResult.DENIED,
                meta=meta,
                details={"reason": "already_running"},
            )
            raise Conflict("Já há uma geração de lições em andamento. Aguarde terminar.")
        async with self._c.sessions() as db:
            latest = await ConfigRepository(db).latest()
        pairs = None if latest is None else latest.document.pairs
        process = await spawn_lessons(
            pairs.timeframe.value if pairs else "1h", tuple(pairs.symbols) if pairs else ()
        )
        self._c.gate_cache[_RUN_KEY] = (time.monotonic(), asyncio.create_task(_supervise(process)))
        await self._audit.record(
            actor=actor,
            action=AuditAction.AI_LESSON_RUN_REQUESTED,
            target="ai_lessons",
            result=AuditResult.SUCCESS,
            meta=meta,
        )
        return LessonRunOut(
            state="started",
            message="Gerando o relatório de lições. Ele aparece na lista como pendente em instantes.",
        )

    async def value(self, days: int) -> AiValueOut:
        days = min(max(days, 1), MAX_DAYS)
        key = f"ai_value:{days}"
        cached = self._c.gate_cache.get(key)
        now = time.monotonic()
        if cached is not None and now - cached[0] < _VALUE_TTL_SECONDS:
            out: AiValueOut = cached[1]
            return out
        report = await measure_ai_value(self._c.sessions, days=days, now=self._c.clock())
        out = AiValueOut(
            **report.model_dump(exclude={"cases"}),
            small_sample=report.simulated < MIN_RESOLVED,
            cases=[VetoCaseOut.model_validate(c.model_dump()) for c in report.cases],
        )
        self._c.gate_cache[key] = (now, out)
        return out
