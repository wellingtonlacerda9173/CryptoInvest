"""Contrato das lições e do valor da IA. Todo texto do modelo é DADO: o painel o exibe como texto."""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


LessonStatus = Literal["pending", "approved", "rejected"]
LessonSource = Literal["ai", "deterministic"]


class ObservationOut(Out):
    text: str
    evidence: str = Field(description="Números que sustentam a observação.")
    confidence: Literal["low", "medium", "high"]


class LessonGroupOut(Out):
    key: str
    trades: int
    wins: int
    losses: int
    total_r: float
    mean_r: float
    loss_r: float
    worst_r: float
    max_drawdown_r: float


class LossOut(Out):
    strategy: str
    symbol: str
    regime: str
    closed_on: date
    r_multiple: float


class LessonStatsOut(Out):
    trades: int
    wins: int
    losses: int
    total_r: float
    loss_r: float
    skipped: int
    by_strategy: list[LessonGroupOut]
    by_regime: list[LessonGroupOut]
    by_symbol: list[LessonGroupOut]
    worst_losses: list[LossOut]
    decisions: dict[str, int]


class LessonOut(Out):
    id: str
    created_at: datetime
    period_start: datetime
    period_end: datetime
    source: LessonSource = Field(description="'ai' (texto do modelo) ou 'deterministic' (só estatísticas).")
    model: str | None
    prompt_version: str
    status: LessonStatus = Field(description="Só 'approved' entra como contexto de leitura da IA.")
    reviewer: str | None
    review_reason: str | None
    reviewed_at: datetime | None
    summary: str
    observations: list[ObservationOut]


class LessonDetailOut(LessonOut):
    stats: LessonStatsOut


class LessonPage(Out):
    items: list[LessonOut]
    next_cursor: str | None = None


class LessonReviewRequest(Out):
    decision: Literal["approve", "reject"]
    reason: str = Field(min_length=3, max_length=500, description="Por que aprova ou rejeita (obrigatório).")


class LessonRunOut(Out):
    state: Literal["started"]
    message: str


class VetoCaseOut(Out):
    decided_at: datetime
    code: str
    strategy: str
    symbol: str
    status: str
    pnl: float
    r_multiple: float


VerdictName = Literal[
    "no_activity",
    "insufficient_sample",
    "no_conclusion",
    "ai_avoided_losses",
    "ai_missed_gains",
    "no_difference",
]


class AiValueOut(Out):
    period_start: datetime
    period_end: datetime
    calls: int
    failed_calls: int
    cost_usd: float
    vetoes: int
    simulated: int
    not_filled: int
    unresolved: int
    not_simulated: int
    avoided_loss: float = Field(description="Perdas que os vetos teriam causado (simulação).")
    missed_gain: float = Field(description="Ganhos que os vetos barraram (simulação).")
    net_avoided: float
    correct_vetoes: int
    false_vetoes: int
    false_veto_rate: float | None
    mean_r_vetoed: float | None
    approved_trades: int
    approved_wins: int
    approved_losses: int
    approved_pnl: float
    approved_loss_pnl: float
    approved_total_r: float
    net_value_usd: float
    verdict: VerdictName
    verdict_text: str
    small_sample: bool
    cases: list[VetoCaseOut]
