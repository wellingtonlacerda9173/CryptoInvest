"""Diario de licoes: resumo estruturado SEM dados sensiveis, relatorio validado e contexto aprovado.

Nada daqui chega ao risco, as regras ou aos scores: licoes aprovadas por um admin so viram texto de leitura
(sanitizado, limitado e marcado como nao confiavel) no pedido do analista.
"""

import json
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Annotated, Literal, Protocol

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StrictStr, ValidationError

from engine.application.ai.models import MAX_LESSON_CHARS, MAX_LESSONS, MAX_LESSONS_TOTAL_CHARS
from engine.application.ai.sanitize import sanitize_text
from engine.application.records import Record
from engine.application.selector import ScoreKey, StrategyScore, TradeSample, max_drawdown_r

LESSON_PROMPT_VERSION = "lessons-v1"
MAX_OBSERVATIONS = 6
MAX_RAW_CHARS = 4000
TOP_LOSSES = 5
TOP_SCORES = 12
SMALL_SAMPLE = 30
TINY_SAMPLE = 10

Confidence = Literal["low", "medium", "high"]
Source = Literal["ai", "deterministic"]
ReviewStatus = Literal["pending", "approved", "rejected"]
_DIGIT = re.compile(r"\d")


def _clean(limit: int) -> AfterValidator:
    return AfterValidator(lambda v: sanitize_text(v, limit))


def _numeric(value: str) -> str:
    if not _DIGIT.search(value):
        raise ValueError("a evidencia precisa citar numeros")
    return value


class GroupStats(Record):
    key: str
    trades: int
    wins: int
    losses: int
    total_r: float
    mean_r: float
    loss_r: float
    worst_r: float
    max_drawdown_r: float


class LossItem(Record):
    strategy: str
    symbol: str
    regime: str
    closed_on: date
    r_multiple: float


class ScoreLine(Record):
    strategy: str
    regime: str
    symbol: str
    trades: int
    score: float
    eligible: bool


class LessonSummary(Record):
    """Tudo o que o relatorio (e a IA) enxerga: estatisticas em R e contagens. Sem saldo, tamanhos de
    posicao, precos, chaves, usuarios ou ids de run. Perdas entram com o mesmo peso dos ganhos."""

    period_start: datetime
    period_end: datetime
    trades: int
    wins: int
    losses: int
    total_r: float
    loss_r: float
    by_strategy: tuple[GroupStats, ...]
    by_regime: tuple[GroupStats, ...]
    by_symbol: tuple[GroupStats, ...]
    decisions: dict[str, int]
    skipped: int
    worst_losses: tuple[LossItem, ...]
    scores: tuple[ScoreLine, ...]


def group_stats(key: str, r_values: Sequence[float]) -> GroupStats:
    losses = [r for r in r_values if r < 0]
    count = len(r_values)
    return GroupStats(
        key=key,
        trades=count,
        wins=sum(1 for r in r_values if r > 0),
        losses=len(losses),
        total_r=round(sum(r_values), 4),
        mean_r=round(sum(r_values) / count, 4) if count else 0.0,
        loss_r=round(sum(losses), 4),
        worst_r=round(min(r_values), 4) if count else 0.0,
        max_drawdown_r=round(max_drawdown_r(list(r_values)), 4),
    )


def _grouped(samples: Sequence[TradeSample], key: str) -> tuple[GroupStats, ...]:
    groups: dict[str, list[float]] = defaultdict(list)
    for sample in sorted(samples, key=lambda s: s.closed_at):
        label = sample.regime.value if key == "regime" else getattr(sample, key)
        groups[str(label)].append(sample.r_multiple)
    return tuple(group_stats(name, groups[name]) for name in sorted(groups))


def build_summary(
    samples: Sequence[TradeSample],
    decisions: Mapping[str, int],
    scores: Mapping[ScoreKey, StrategyScore],
    *,
    start: datetime,
    end: datetime,
) -> LessonSummary:
    window = [s for s in samples if start <= s.closed_at <= end]
    overall = group_stats("total", [s.r_multiple for s in sorted(window, key=lambda s: s.closed_at)])
    losses = sorted((s for s in window if s.r_multiple < 0), key=lambda s: (s.r_multiple, s.closed_at))
    ranked = sorted(scores.values(), key=lambda s: (-s.score, s.strategy, s.symbol, s.regime.value))
    return LessonSummary(
        period_start=start,
        period_end=end,
        trades=overall.trades,
        wins=overall.wins,
        losses=overall.losses,
        total_r=overall.total_r,
        loss_r=overall.loss_r,
        by_strategy=_grouped(window, "strategy"),
        by_regime=_grouped(window, "regime"),
        by_symbol=_grouped(window, "symbol"),
        decisions=dict(sorted(decisions.items())),
        skipped=sum(decisions.values()),
        worst_losses=tuple(
            LossItem(
                strategy=s.strategy,
                symbol=s.symbol,
                regime=s.regime.value,
                closed_on=s.closed_at.date(),
                r_multiple=round(s.r_multiple, 4),
            )
            for s in losses[:TOP_LOSSES]
        ),
        scores=tuple(
            ScoreLine(
                strategy=s.strategy,
                regime=s.regime.value,
                symbol=s.symbol,
                trades=s.trades,
                score=round(s.score, 4),
                eligible=s.eligible,
            )
            for s in ranked[:TOP_SCORES]
        ),
    )


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Observation(_Strict):
    text: Annotated[StrictStr, Field(min_length=1, max_length=240), _clean(240)]
    evidence: Annotated[StrictStr, Field(min_length=1, max_length=160), AfterValidator(_numeric), _clean(160)]
    confidence: Confidence


class LessonReport(_Strict):
    summary: Annotated[StrictStr, Field(min_length=1, max_length=300), _clean(300)]
    observations: tuple[Observation, ...] = Field(min_length=1, max_length=MAX_OBSERVATIONS)


def parse_report(raw: str) -> LessonReport | None:
    """So um objeto JSON puro dentro do schema; qualquer desvio => None (cai no relatorio deterministico)."""
    if not isinstance(raw, str) or len(raw) > MAX_RAW_CHARS:
        return None
    try:
        return LessonReport.model_validate(json.loads(raw.strip(), parse_constant=_reject))
    except (ValueError, ValidationError):
        return None


def _reject(name: str) -> float:
    raise ValueError(name)


def _confidence(trades: int) -> Confidence:
    return "low" if trades < TINY_SAMPLE else "medium" if trades < SMALL_SAMPLE else "high"


def _line(stats: GroupStats) -> Observation:
    return Observation(
        text=f"{stats.key}: {stats.wins} ganhos e {stats.losses} perdas em {stats.trades} operações.",
        evidence=(
            f"total {stats.total_r:+.2f}R, média {stats.mean_r:+.2f}R, perdas {stats.loss_r:+.2f}R, "
            f"pior {stats.worst_r:+.2f}R, drawdown {stats.max_drawdown_r:.2f}R"
        ),
        confidence=_confidence(stats.trades),
    )


def deterministic_report(summary: LessonSummary) -> LessonReport:
    """Relatorio so com estatisticas (sem IA). Perdas e a pior operacao SEMPRE aparecem quando existem."""
    if summary.trades == 0:
        return LessonReport(
            summary="Sem operações encerradas no período: nada a aprender ainda.",
            observations=(
                Observation(
                    text="Nenhuma operação encerrada no período.",
                    evidence=f"0 operações, {summary.skipped} sinais não operados",
                    confidence="low",
                ),
            ),
        )
    observations = [_line(stats) for stats in summary.by_strategy[: MAX_OBSERVATIONS - 3]]
    if summary.worst_losses:
        worst = summary.worst_losses[0]
        observations.append(
            Observation(
                text=f"Maior perda: {worst.strategy} em {worst.symbol} ({worst.regime}).",
                evidence=(
                    f"{worst.r_multiple:+.2f}R em {worst.closed_on:%d/%m/%Y}; "
                    f"{summary.losses} perdas no total"
                ),
                confidence=_confidence(summary.trades),
            )
        )
    if summary.skipped:
        top = max(summary.decisions.items(), key=lambda item: item[1])
        observations.append(
            Observation(
                text=f"Sinais não operados: o motivo mais comum foi {top[0]}.",
                evidence=f"{summary.skipped} sinais não operados, {top[1]} por {top[0]}",
                confidence="medium",
            )
        )
    return LessonReport(
        summary=(
            f"{summary.trades} operações: {summary.wins} ganhos, {summary.losses} perdas, "
            f"resultado {summary.total_r:+.2f}R (perdas {summary.loss_r:+.2f}R)."
        ),
        observations=tuple(observations[:MAX_OBSERVATIONS]),
    )


class StoredLesson(Record):
    id: str
    created_at: datetime
    period_start: datetime
    period_end: datetime
    source: Source
    model: str | None = None
    prompt_version: str
    summary: dict[str, object]
    report: LessonReport
    status: ReviewStatus = "pending"
    reviewer: str | None = None
    review_reason: str | None = None
    reviewed_at: datetime | None = None


class LessonSource(Protocol):
    async def approved(self, limit: int) -> Sequence[StoredLesson]: ...


def lesson_notes(lessons: Sequence[StoredLesson]) -> tuple[str, ...]:
    """Notas de leitura para o analista: SO licoes cuja revisao mais recente e 'approved', texto
    sanitizado de novo na saida, no maximo `MAX_LESSONS` notas e `MAX_LESSONS_TOTAL_CHARS` caracteres."""
    notes: list[str] = []
    used = 0
    for lesson in sorted(lessons, key=lambda item: item.created_at, reverse=True):
        if lesson.status != "approved":
            continue
        for item in lesson.report.observations:
            note = sanitize_text(
                f"{item.text} ({item.evidence}; confiança {item.confidence})", MAX_LESSON_CHARS
            )
            if not note or len(notes) >= MAX_LESSONS or used + len(note) > MAX_LESSONS_TOTAL_CHARS:
                return tuple(notes)
            notes.append(note)
            used += len(note)
    return tuple(notes)
