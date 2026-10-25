"""Contrato do analista de IA: so dados de mercado e do sinal entram; so um parecer estrito sai.

O request NAO tem (e `extra="forbid"` impede) saldo, posicoes, chaves, identidade ou run_id. O parecer so
pode reduzir a confianca do sinal ou veta-lo (ver `combine`); nunca toca em risco, tamanho, stop ou alvo.
"""

import hashlib
import json
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StrictFloat, StrictStr, model_validator

from engine.application.ai.sanitize import sanitize_text

PROMPT_VERSION = "analyst-v2"
MAX_REASON_CHARS = 280
MAX_CANDLES = 24
MAX_ITEMS = 8
MAX_LESSONS = 5
MAX_LESSON_CHARS = 240
MAX_LESSONS_TOTAL_CHARS = 1000

Verdict = Literal["approve", "veto"]


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _text(limit: int) -> AfterValidator:
    return AfterValidator(lambda v: sanitize_text(v, limit))


ShortText = Annotated[StrictStr, _text(60)]
Number = Annotated[float, Field(allow_inf_nan=False)]


class CandleView(_Strict):
    ago: int = Field(ge=0, le=500)
    open: Number
    high: Number
    low: Number
    close: Number
    volume: Number


class IndicatorsView(_Strict):
    rsi14: Number | None = None
    adx14: Number | None = None
    atr14_pct: Number | None = None
    volume_ratio: Number | None = None
    ema20_vs_ema50_pct: Number | None = None


class SignalView(_Strict):
    strategy: ShortText
    entry: Number
    stop: Number
    target: Number
    reward_risk: Number
    confidence: float = Field(ge=0, le=1)
    confluences: tuple[ShortText, ...] = Field(default=(), max_length=MAX_ITEMS)


class ScoreView(_Strict):
    strategy: ShortText
    score: Number


class AnalystRequest(_Strict):
    symbol: ShortText
    timeframe: ShortText
    regime: ShortText
    candles: tuple[CandleView, ...] = Field(max_length=MAX_CANDLES)
    indicators: IndicatorsView
    signal: SignalView
    selector_scores: tuple[ScoreView, ...] = Field(default=(), max_length=MAX_ITEMS)
    lessons: tuple[Annotated[StrictStr, _text(MAX_LESSON_CHARS)], ...] = Field(
        default=(), max_length=MAX_LESSONS
    )

    @model_validator(mode="after")
    def _lessons_budget(self) -> "AnalystRequest":
        if sum(len(item) for item in self.lessons) > MAX_LESSONS_TOTAL_CHARS:
            raise ValueError("licoes acima do limite total de texto")
        return self

    def payload(self) -> dict[str, object]:
        """O que vai ao modelo. Sem licoes aprovadas o corpo e identico ao de antes (chave ausente)."""
        return self.model_dump(mode="json", exclude=None if self.lessons else {"lessons"})

    def digest(self) -> str:
        raw = json.dumps(self.payload(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()


class AnalystVerdict(_Strict):
    """Unica saida aceita do modelo. Qualquer campo a mais/a menos, tipo errado ou texto longo => invalido."""

    verdict: Verdict
    confidence: StrictFloat = Field(ge=0, le=1)
    reason: Annotated[StrictStr, Field(min_length=1, max_length=MAX_REASON_CHARS)]


class AnalystOpinion(_Strict):
    verdict: Verdict
    confidence: float = Field(ge=0, le=1)
    reason: Annotated[str, Field(min_length=1, max_length=MAX_REASON_CHARS), _text(MAX_REASON_CHARS)]
    model: str = Field(min_length=1, max_length=64)
    prompt_version: str = Field(min_length=1, max_length=32)

    def to_insight(self) -> dict[str, object]:
        return {**self.model_dump(mode="json"), "untrusted": True}

    def summary(self) -> str:
        """Texto para o painel (que o escapa): a justificativa e sempre dado nao confiavel."""
        what = "Vetou" if self.verdict == "veto" else "Aprovou"
        return f"{what} (confiança {self.confidence:.0%}): {self.reason}"


CombineCode = Literal["ai_veto", "ai_low_confidence"]


class CombinedResult(_Strict):
    approved: bool
    confidence: float = Field(ge=0, le=1)
    code: CombineCode | None = None
