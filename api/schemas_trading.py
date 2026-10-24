"""Contrato das leituras de trading. Valores monetários e percentuais são números para EXIBIÇÃO (sem
aritmética no painel); o CSV de operações usa os decimais exatos. Nenhum modelo aqui carrega segredo."""

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Mode = Literal["paper", "live", "backtest"]
BotStateName = Literal[
    "STARTING", "RUNNING", "PAUSED", "FAILED_PERSISTENCE", "HALTED", "STOPPING", "STOPPED", "OFFLINE"
]
Connection = Literal["live", "stale", "offline", "unknown"]
Level = Literal["ok", "warn", "bad"]
CriterionStatus = Literal["met", "failed", "in_progress"]


class Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PauseReasonOut(Out):
    reason: str
    operator: str
    at: datetime


class RiskLimitOut(Out):
    key: str = Field(examples=["daily_loss"])
    name: str = Field(examples=["Perda diária"])
    value: float
    limit: float
    unit: Literal["%", ""]
    level: Level
    status_text: str = Field(examples=["Folgado"])
    summary: str = Field(examples=["Perda diária: 0,3% de 1% - Folgado"])
    note: str
    active_until: datetime | None = Field(default=None, description="Trava ativa: até quando.")


class LiveEligibilityOut(Out):
    eligible: bool = Field(description="O run ainda pode servir de evidência para o Live.")
    approved: bool = Field(description="Todos os critérios da trava do Live foram cumpridos.")
    reason: str | None = None
    criteria_met: int
    criteria_total: int
    live_available: bool = Field(description="Falso nesta fase: o Live ainda não pode ser ativado.")


class BotStatusOut(Out):
    run_id: str | None
    mode: Mode
    state: BotStateName
    state_label: str
    state_detail: str
    connection: Connection = Field(
        description="`live`: sinal recente; `stale`: sem sinal há muito; `offline`."
    )
    heartbeat_at: datetime | None
    heartbeat_age_seconds: float | None
    started_at: datetime | None
    code_version: str | None
    stops_blind: bool
    halted: bool
    halt_reason: str | None
    paused: bool
    pause_reasons: list[PauseReasonOut]
    outbox_pending: int
    persistence_error: str | None
    redis_healthy: bool | None
    allow_open_calendar: bool
    open_positions: list[str]
    pending_entries: list[str]
    equity: float | None
    realized_equity: float | None
    peak_equity: float | None
    drawdown_pct: float | None
    day_pnl: float | None
    day_pnl_pct: float | None
    daily_pause_until: datetime | None
    target_reached_on: date | None
    pair_pause_until: dict[str, datetime]
    config_hash: str | None
    config_version: int | None
    simulated: bool
    live: LiveEligibilityOut
    limits: list[RiskLimitOut]
    pending_commands: int = Field(description="Comandos do painel ainda sem entrega ao robô.")


class AccountSummaryOut(Out):
    mode: Mode
    simulated: bool
    currency: str
    initial_capital: float | None
    equity: float | None
    realized_equity: float | None
    day_pnl: float | None
    day_pnl_pct: float | None
    week_pnl: float | None
    week_pnl_pct: float | None
    total_pnl: float | None
    total_pnl_pct: float | None
    as_of: datetime | None


class EquityPointOut(Out):
    at: datetime
    equity: float
    realized: float


class EquitySeriesOut(Out):
    mode: Mode
    simulated: bool
    initial_capital: float | None
    points: list[EquityPointOut]


class PositionOut(Out):
    run_id: str
    mode: Mode
    symbol: str
    quantity: float
    entry_price: float
    stop: float
    initial_stop: float
    take_profit: float | None
    last_price: float | None
    unrealized_pnl: float | None
    unrealized_r: float | None
    risk_amount: float
    opened_at: datetime
    breakeven_reached: bool
    partial_done: bool
    protected: bool = Field(description="Falso quando o robô está sem dados de mercado (stops cegos).")
    protection_note: str


class TradeOut(Out):
    id: int
    trade_id: str
    run_id: str
    mode: Mode
    symbol: str
    strategy: str | None
    opened_at: datetime
    closed_at: datetime
    entry_price: float
    exit_price: float
    quantity: float
    pnl: float
    fees: float
    r_multiple: float | None
    exit_reason: str
    exit_reason_text: str
    result: Literal["win", "loss", "breakeven"]


class TradesSummaryOut(Out):
    count: int
    wins: int
    losses: int
    pnl: float
    fees: float


class TradePage(Out):
    items: list[TradeOut]
    next_cursor: int | None = Field(default=None, description="Passe em `cursor` para a próxima página.")
    summary: TradesSummaryOut = Field(description="Totais do filtro aplicado (um único modo, sem mistura).")


class DecisionOut(Out):
    id: int
    run_id: str
    mode: Mode
    at: datetime
    symbol: str
    action: Literal["enter", "exit", "hold", "skip"]
    traded: bool
    outcome: str = Field(description="Palavra curta: Entrou, Não entrou, Esperou.")
    outcome_kind: Literal["entered", "waited", "refused", "info"]
    code: str
    reason_text: str
    detail: str = Field(description="Mensagem original do motor (técnica).")
    strategy: str | None
    confidence: float | None
    signal: dict[str, Any] | None


class DecisionSummaryOut(Out):
    day: date
    signals: int
    entries: int
    refusals: int
    waits: int


class DecisionPage(Out):
    items: list[DecisionOut]
    next_cursor: int | None = None
    summary: DecisionSummaryOut | None = Field(default=None, description="Resumo do dia (só na 1ª página).")


class GroupStatsOut(Out):
    key: str
    trades: int
    wins: int
    pnl: float
    win_rate: float | None
    profit_factor: float | None


class AvoidedOut(Out):
    code: str
    label: str
    count: int


class PerformanceOut(Out):
    run_id: str | None
    mode: Mode
    simulated: bool
    period_start: datetime | None
    period_end: datetime
    trades: int
    wins: int
    losses: int
    win_rate: float | None
    profit_factor: float | None
    payoff: float | None
    expectancy_r: float | None
    total_pnl: float
    total_return_pct: float | None
    max_drawdown_pct: float | None
    sharpe: float | None
    idle_pct: float | None = Field(description="Tempo sem posição aberta (não é falha).")
    positive_months: int
    months: int
    by_strategy: list[GroupStatsOut]
    by_pair: list[GroupStatsOut]
    by_month: list[GroupStatsOut]
    avoided: list[AvoidedOut]
    signals_refused: int
    sample_warning: str | None
    notes: list[str]


class GateCriterionOut(Out):
    code: str
    name: str
    status: CriterionStatus
    value_text: str
    target_text: str
    value: float | None
    target: float | None
    missing_text: str | None = None
    reason: str | None = None


class LiveGateOut(Out):
    run_id: str | None
    mode: Mode
    approved: bool
    eligible_for_live: bool
    verdict: str
    ineligible_reason: str | None
    criteria: list[GateCriterionOut]
    phrase_required: str
    live_available: bool
    unavailable_reason: str
    first_activation_reduced_risk_days: int


class CandleOut(Out):
    t: datetime
    o: float
    h: float
    l: float  # noqa: E741
    c: float
    v: float


class MarkerOut(Out):
    t: datetime
    kind: Literal["entry", "exit", "stop", "target", "elephant"]
    price: float | None
    text: str
    run_id: str | None = None


class LevelOut(Out):
    kind: Literal["stop", "target", "entry"]
    price: float
    since: datetime


class CandlesOut(Out):
    symbol: str
    timeframe: str
    mode: Mode
    simulated: bool
    candles: list[CandleOut]
    markers: list[MarkerOut]
    levels: list[LevelOut]
    last_price: float | None
    change_24h_pct: float | None
    volume_24h: float | None
    last_decision: str | None
    blind_since: datetime | None = None


class GlossaryItemOut(Out):
    term: str
    short: str
    long: str


class StateWordOut(Out):
    state: str
    label: str
    detail: str


class ReasonWordOut(Out):
    code: str
    label: str
    text: str


class GlossaryOut(Out):
    terms: list[GlossaryItemOut]
    states: list[StateWordOut]
    reasons: list[ReasonWordOut]
