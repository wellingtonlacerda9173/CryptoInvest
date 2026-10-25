"""Relatorio de licoes pelo modelo: mesma estrada do analista (orcamento duro, circuito, auditoria)."""

import json

from engine.application.ai.lessons import (
    LESSON_PROMPT_VERSION,
    LessonReport,
    LessonSummary,
    parse_report,
)
from engine.infrastructure.ai.analyst import AnthropicAnalyst
from engine.infrastructure.ai.prompt import LESSONS_SYSTEM_PROMPT


class AnthropicLessonWriter:
    def __init__(self, analyst: AnthropicAnalyst) -> None:
        self._analyst = analyst

    @property
    def model(self) -> str:
        return str(self._analyst.status()["model"])

    async def write(self, summary: LessonSummary, *, decision_key: str) -> LessonReport | None:
        payload = {"untrusted_summary": summary.model_dump(mode="json")}
        user = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return await self._analyst.complete(
            LESSONS_SYSTEM_PROMPT,
            user,
            prompt_version=LESSON_PROMPT_VERSION,
            parse=parse_report,
            decision_key=decision_key,
        )
