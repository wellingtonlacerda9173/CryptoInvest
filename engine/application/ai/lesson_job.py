"""Job do diario de licoes: (a) reavalia os scores sem IA, (b) resume, (c) relata, (d) grava como pendente."""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Protocol

from engine.application.ai.lessons import (
    LESSON_PROMPT_VERSION,
    LessonReport,
    LessonSummary,
    Source,
    build_summary,
    deterministic_report,
)
from engine.application.selector import SelectorSettings, StrategyScore, TradeSample, build_scores

Origin = Literal["paper", "live"]
DEFAULT_PERIOD_DAYS = 7


class LessonWriter(Protocol):
    async def write(self, summary: LessonSummary, *, decision_key: str) -> LessonReport | None:
        """Relatorio da IA, ou None (sem IA, sem orcamento, circuito aberto, resposta invalida)."""
        ...

    @property
    def model(self) -> str: ...


@dataclass(frozen=True, slots=True)
class NewLesson:
    created_at: datetime
    period_start: datetime
    period_end: datetime
    source: Source
    model: str | None
    prompt_version: str
    summary: LessonSummary
    report: LessonReport


@dataclass(frozen=True, slots=True)
class LessonJobResult:
    lesson_id: str
    source: Source
    scores: int
    snapshot_id: str | None
    trades: int


async def run_lesson_job(
    *,
    journal: Sequence[TradeSample],
    backtest_samples: Sequence[TradeSample] = (),
    decisions: Mapping[str, int],
    settings: SelectorSettings,
    now: datetime,
    publish: Callable[[Sequence[StrategyScore], Origin], Awaitable[str]],
    save: Callable[[NewLesson], Awaitable[str]],
    writer: LessonWriter | None,
    period_days: int = DEFAULT_PERIOD_DAYS,
) -> LessonJobResult:
    """Scores: deterministico, mesmo seletor do robo, snapshot novo (append-only). A IA so escreve texto:
    nao ve nem altera scores, e sem ela o relatorio sai so com estatisticas."""
    pool = [*backtest_samples, *journal]
    scores = build_scores(pool, now, settings.selector, lookback_days=settings.lookback_days) if pool else {}
    snapshot: str | None = None
    if scores:
        origin: Origin = "live" if any(s.source == "live" for s in journal) else "paper"
        snapshot = await publish(list(scores.values()), origin)
    summary = build_summary(journal, decisions, scores, start=now - timedelta(days=period_days), end=now)
    report: LessonReport | None = None
    if writer is not None and summary.trades > 0:
        report = await writer.write(summary, decision_key=f"lessons:{now:%y%m%d%H%M}")
    source: Source = "ai" if report is not None else "deterministic"
    lesson_id = await save(
        NewLesson(
            created_at=now,
            period_start=summary.period_start,
            period_end=summary.period_end,
            source=source,
            model=writer.model if report is not None and writer is not None else None,
            prompt_version=LESSON_PROMPT_VERSION if report is not None else "deterministic-v1",
            summary=summary,
            report=report or deterministic_report(summary),
        )
    )
    return LessonJobResult(lesson_id, source, len(scores), snapshot, summary.trades)
