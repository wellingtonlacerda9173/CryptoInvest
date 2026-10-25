from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

PRICE = Numeric(38, 18)


class Base(DeclarativeBase):
    pass


class CandleRow(Base):
    """Hypertable Timescale particionada por open_time. Somente velas fechadas."""

    __tablename__ = "candles"
    __table_args__ = (
        CheckConstraint(
            "high >= greatest(open, close) AND low <= least(open, close)", name="ck_candles_ohlc"
        ),
        CheckConstraint("volume >= 0", name="ck_candles_volume"),
        CheckConstraint("open > 0 AND high > 0 AND low > 0 AND close > 0", name="ck_candles_positive_prices"),
        # criado automaticamente pelo create_hypertable da migracao 0001
        Index("candles_open_time_idx", text("open_time DESC")),
    )

    symbol: Mapped[str] = mapped_column(String(25), primary_key=True)
    timeframe: Mapped[str] = mapped_column(String(4), primary_key=True)
    open_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    open: Mapped[Decimal] = mapped_column(PRICE)
    high: Mapped[Decimal] = mapped_column(PRICE)
    low: Mapped[Decimal] = mapped_column(PRICE)
    close: Mapped[Decimal] = mapped_column(PRICE)
    volume: Mapped[Decimal] = mapped_column(PRICE)


class AuditLogRow(Base):
    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_log_occurred_at", "occurred_at"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    actor: Mapped[str] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(100))
    entity: Mapped[str] = mapped_column(String(100))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class RunRow(Base):
    __tablename__ = "runs"
    __table_args__ = (CheckConstraint("mode IN ('paper', 'live', 'backtest')", name="ck_runs_mode"),)

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    mode: Mapped[str] = mapped_column(String(10))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    config_hash: Mapped[str] = mapped_column(String(64))
    code_version: Mapped[str] = mapped_column(String(64))


class OrderRow(Base):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("run_id", "client_order_id", name="uq_orders_run_client_order_id"),
        CheckConstraint("side IN ('buy', 'sell')", name="ck_orders_side"),
        CheckConstraint("quantity > 0", name="ck_orders_quantity"),
        Index("ix_orders_run_status", "run_id", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    client_order_id: Mapped[str] = mapped_column(String(64))
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id"))
    symbol: Mapped[str] = mapped_column(String(25))
    side: Mapped[str] = mapped_column(String(4))
    type: Mapped[str] = mapped_column(String(10))
    quantity: Mapped[Decimal] = mapped_column(PRICE)
    price: Mapped[Decimal | None] = mapped_column(PRICE)
    stop_loss: Mapped[Decimal | None] = mapped_column(PRICE)
    take_profit: Mapped[Decimal | None] = mapped_column(PRICE)
    status: Mapped[str] = mapped_column(String(20))
    exchange_order_id: Mapped[str | None] = mapped_column(String(64))
    filled_quantity: Mapped[Decimal] = mapped_column(PRICE, server_default=text("0"))
    average_price: Mapped[Decimal | None] = mapped_column(PRICE)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class FillRow(Base):
    __tablename__ = "fills"
    __table_args__ = (
        UniqueConstraint("run_id", "fill_id", name="uq_fills_run_fill_id"),
        CheckConstraint("side IN ('buy', 'sell')", name="ck_fills_side"),
        CheckConstraint("quantity > 0 AND price > 0 AND fee >= 0", name="ck_fills_positive"),
        Index("ix_fills_run_filled_at", "run_id", "filled_at"),
        Index("ix_fills_client_order_id", "run_id", "client_order_id"),
        ForeignKeyConstraint(
            ["run_id", "client_order_id"],
            ["orders.run_id", "orders.client_order_id"],
            name="fk_fills_run_order",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    fill_id: Mapped[str] = mapped_column(String(64))
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id"))
    client_order_id: Mapped[str] = mapped_column(String(64))
    symbol: Mapped[str] = mapped_column(String(25))
    side: Mapped[str] = mapped_column(String(4))
    quantity: Mapped[Decimal] = mapped_column(PRICE)
    price: Mapped[Decimal] = mapped_column(PRICE)
    fee: Mapped[Decimal] = mapped_column(PRICE)
    fee_asset: Mapped[str | None] = mapped_column(String(12))
    filled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PositionRow(Base):
    __tablename__ = "positions"
    __table_args__ = (
        CheckConstraint("quantity > 0 AND initial_quantity >= quantity", name="ck_positions_quantity"),
        CheckConstraint("stop >= initial_stop AND initial_stop < entry_price", name="ck_positions_stops"),
        # uma posicao aberta por simbolo e run
        Index(
            "uq_positions_open_symbol",
            "run_id",
            "symbol",
            unique=True,
            postgresql_where=text("closed_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id"))
    symbol: Mapped[str] = mapped_column(String(25))
    entry_price: Mapped[Decimal] = mapped_column(PRICE)
    initial_quantity: Mapped[Decimal] = mapped_column(PRICE)
    quantity: Mapped[Decimal] = mapped_column(PRICE)
    initial_stop: Mapped[Decimal] = mapped_column(PRICE)
    stop: Mapped[Decimal] = mapped_column(PRICE)
    take_profit: Mapped[Decimal | None] = mapped_column(PRICE)
    highest_price: Mapped[Decimal] = mapped_column(PRICE)
    entry_fee: Mapped[Decimal] = mapped_column(PRICE)
    breakeven_reached: Mapped[bool] = mapped_column(Boolean)
    partial_done: Mapped[bool] = mapped_column(Boolean)
    realized_pnl: Mapped[Decimal] = mapped_column(PRICE)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TradeRow(Base):
    """Ledger append-only (trigger no banco): perdas jamais sao alteradas, apagadas ou reclassificadas."""

    __tablename__ = "trades"
    __table_args__ = (
        UniqueConstraint("run_id", "trade_id", name="uq_trades_run_trade_id"),
        CheckConstraint("closed_at >= opened_at", name="ck_trades_times"),
        CheckConstraint(
            "exit_reason IN ('stop_loss', 'take_profit', 'profit_protection', 'signal', 'manual')",
            name="ck_trades_exit_reason",
        ),
        Index("ix_trades_run_closed_at", "run_id", "closed_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    trade_id: Mapped[str] = mapped_column(String(64))
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id"))
    symbol: Mapped[str] = mapped_column(String(25))
    quantity: Mapped[Decimal] = mapped_column(PRICE)
    entry_price: Mapped[Decimal] = mapped_column(PRICE)
    exit_price: Mapped[Decimal] = mapped_column(PRICE)
    entry_fee: Mapped[Decimal] = mapped_column(PRICE)
    exit_fee: Mapped[Decimal] = mapped_column(PRICE)
    pnl: Mapped[Decimal] = mapped_column(PRICE)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    exit_reason: Mapped[str] = mapped_column(String(20))
    strategy: Mapped[str | None] = mapped_column(String(100))
    risk_amount: Mapped[Decimal | None] = mapped_column(PRICE)
    r_multiple: Mapped[Decimal | None] = mapped_column(PRICE)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DecisionRow(Base):
    """Feed de decisoes, append-only (trigger no banco)."""

    __tablename__ = "decisions"
    __table_args__ = (
        UniqueConstraint("run_id", "decision_key", name="uq_decisions_run_decision_key"),
        Index("ix_decisions_run_decided_at", "run_id", "decided_at"),
        Index("ix_decisions_signal_id", "signal_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    decision_key: Mapped[str] = mapped_column(String(48))
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id"))
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    symbol: Mapped[str] = mapped_column(String(25))
    action: Mapped[str] = mapped_column(String(10))
    traded: Mapped[bool] = mapped_column(Boolean)
    code: Mapped[str] = mapped_column(String(50))
    reason: Mapped[str] = mapped_column(Text)
    strategy: Mapped[str | None] = mapped_column(String(100))
    confidence: Mapped[float | None] = mapped_column(Float)
    signal_id: Mapped[str | None] = mapped_column(String(64))
    client_order_id: Mapped[str | None] = mapped_column(String(64))
    signal: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    context: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RiskStateRow(Base):
    __tablename__ = "risk_state"
    __table_args__ = (UniqueConstraint("run_id", "version", name="uq_risk_state_run_version"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id"))
    version: Mapped[int] = mapped_column(Integer)
    saved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    state: Mapped[dict[str, Any]] = mapped_column(JSONB)


class GatewayStateRow(Base):
    __tablename__ = "gateway_state"
    __table_args__ = (UniqueConstraint("run_id", "version", name="uq_gateway_state_run_version"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id"))
    version: Mapped[int] = mapped_column(Integer)
    saved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    state: Mapped[dict[str, Any]] = mapped_column(JSONB)


class EquitySnapshotRow(Base):
    """Hypertable Timescale particionada por `at`."""

    __tablename__ = "equity_snapshots"
    __table_args__ = (
        # criado automaticamente pelo create_hypertable da migracao 0003
        Index("equity_snapshots_at_idx", text("at DESC")),
    )

    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id"), primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    equity: Mapped[Decimal] = mapped_column(PRICE)
    realized: Mapped[Decimal] = mapped_column(PRICE)
    in_position: Mapped[bool] = mapped_column(Boolean)


class StrategyScoreRow(Base):
    """Snapshots de scores do seletor, append-only (trigger); o robo le o ultimo por par e timeframe."""

    __tablename__ = "strategy_scores"
    __table_args__ = (
        UniqueConstraint(
            "snapshot_id",
            "strategy",
            "regime",
            "symbol",
            "timeframe",
            name="uq_strategy_scores_snapshot_key",
        ),
        CheckConstraint("origin IN ('backtest', 'paper', 'live')", name="ck_strategy_scores_origin"),
        CheckConstraint("sample_size >= 0", name="ck_strategy_scores_sample"),
        Index("ix_strategy_scores_latest", "timeframe", "symbol", text("calculated_at DESC")),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(String(64))
    strategy: Mapped[str] = mapped_column(String(100))
    regime: Mapped[str] = mapped_column(String(30))
    symbol: Mapped[str] = mapped_column(String(25))
    timeframe: Mapped[str] = mapped_column(String(5))
    sample_size: Mapped[int] = mapped_column(Integer)
    score: Mapped[float] = mapped_column(Float)
    eligible: Mapped[bool] = mapped_column(Boolean)
    ineligible_reason: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    calculated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    origin: Mapped[str] = mapped_column(String(10))
    run_id: Mapped[str | None] = mapped_column(String(64))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AiCallRow(Base):
    """Uma linha por chamada ao analista de IA (inclusive falhas e recusas por orcamento), append-only.

    Sem dados sensiveis: so hashes, texto sanitizado da resposta, tokens e custo estimado.
    """

    __tablename__ = "ai_calls"
    __table_args__ = (
        CheckConstraint(
            "result IN ('ok', 'invalid', 'timeout', 'budget', 'error', 'cancelled', 'reserve')",
            name="ck_ai_calls_result",
        ),
        CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0 AND cost_usd >= 0", name="ck_ai_calls_usage"
        ),
        Index("ix_ai_calls_called_at", text("called_at DESC")),
        Index(
            "ux_ai_calls_reservation",
            "reservation_id",
            unique=True,
            postgresql_where=text("reservation_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    called_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    model: Mapped[str] = mapped_column(String(64))
    prompt_version: Mapped[str] = mapped_column(String(32))
    prompt_hash: Mapped[str] = mapped_column(String(64))
    response_hash: Mapped[str | None] = mapped_column(String(64))
    response_text: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int] = mapped_column(Integer)
    output_tokens: Mapped[int] = mapped_column(Integer)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(14, 6))
    latency_ms: Mapped[int] = mapped_column(Integer)
    result: Mapped[str] = mapped_column(String(10))
    decision_key: Mapped[str | None] = mapped_column(String(64))
    reservation_id: Mapped[int | None] = mapped_column(BigInteger)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AiLessonRow(Base):
    """Relatorio de licoes do job, append-only. O estado de aprovacao vive em `ai_lesson_reviews`."""

    __tablename__ = "ai_lessons"
    __table_args__ = (
        CheckConstraint("source IN ('ai', 'deterministic')", name="ck_ai_lessons_source"),
        CheckConstraint("period_end >= period_start", name="ck_ai_lessons_period"),
        Index("ix_ai_lessons_created", text("created_at DESC"), "id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(14))
    model: Mapped[str | None] = mapped_column(String(64))
    prompt_version: Mapped[str] = mapped_column(String(32))
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB)
    report: Mapped[dict[str, Any]] = mapped_column(JSONB)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AiLessonReviewRow(Base):
    """Decisoes humanas sobre licoes, append-only: a mais recente por licao vale; so admin (CHECK)."""

    __tablename__ = "ai_lesson_reviews"
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'approved', 'rejected')", name="ck_ai_lesson_reviews_status"),
        CheckConstraint("reviewer_role = 'admin'", name="ck_ai_lesson_reviews_admin"),
        CheckConstraint("length(btrim(reason)) >= 3", name="ck_ai_lesson_reviews_reason"),
        Index("ix_ai_lesson_reviews_lesson", "lesson_id", text("id DESC")),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    lesson_id: Mapped[str] = mapped_column(ForeignKey("ai_lessons.id"))
    status: Mapped[str] = mapped_column(String(10))
    reviewer: Mapped[str] = mapped_column(String(64))
    reviewer_role: Mapped[str] = mapped_column(String(10))
    reason: Mapped[str] = mapped_column(Text)
    reviewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ControlEventRow(Base):
    """Acoes de controle (ligar/desligar/pausar/reativar), append-only (trigger no banco)."""

    __tablename__ = "control_events"
    __table_args__ = (
        CheckConstraint(
            "action IN ('start', 'stop', 'pause', 'resume', 'reactivate', 'halt')",
            name="ck_control_events_action",
        ),
        CheckConstraint("btrim(operator) <> '' AND btrim(reason) <> ''", name="ck_control_events_identified"),
        Index("ix_control_events_run_at", "run_id", "at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id"))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    action: Mapped[str] = mapped_column(String(12))
    operator: Mapped[str] = mapped_column(String(100))
    reason: Mapped[str] = mapped_column(Text)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ConfigVersionRow(Base):
    """Versoes da configuracao do robo, append-only (trigger no banco): ator, motivo, diff e hash encadeado.

    `activation`: `immediate` (so reduz risco, aplicada ao robo em execucao) ou `next_start` (vale no
    proximo (re)inicio com o robo sem posicao aberta). `chain_hash` encadeia a versao anterior (evidencia
    de adulteracao).
    """

    __tablename__ = "config_versions"
    __table_args__ = (
        CheckConstraint("activation IN ('immediate', 'next_start')", name="ck_config_versions_activation"),
        CheckConstraint("btrim(actor) <> '' AND btrim(reason) <> ''", name="ck_config_versions_identified"),
        UniqueConstraint("parent_id", name="uq_config_versions_parent"),
        # so UMA raiz: o UNIQUE(parent_id) nao vale para NULL (a 1a versao concorrente duplicaria a raiz)
        Index(
            "uq_config_versions_single_root",
            text("(true)"),
            unique=True,
            postgresql_where=text("parent_id IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    parent_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("config_versions.id", name="fk_config_versions_parent")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    actor: Mapped[str] = mapped_column(String(100))
    reason: Mapped[str] = mapped_column(Text)
    sections: Mapped[list[str]] = mapped_column(JSONB)
    activation: Mapped[str] = mapped_column(String(12))
    risk_increase: Mapped[bool] = mapped_column(Boolean)
    document: Mapped[dict[str, Any]] = mapped_column(JSONB)
    diff: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    config_hash: Mapped[str] = mapped_column(String(64))
    chain_hash: Mapped[str] = mapped_column(String(64))


class ControlCommandRow(Base):
    """Comandos do painel ao runner. Fila duravel: `pending_delivery` ate haver canal (Redis/arquivo)."""

    __tablename__ = "control_commands"
    __table_args__ = (
        CheckConstraint(
            "state IN ('pending_delivery', 'delivered', 'applied', 'partially_applied', 'refused', "
            "'discarded', 'expired', 'failed')",
            name="ck_control_commands_state",
        ),
        Index("ix_control_commands_state", "state"),
        Index("ix_control_commands_created_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(20))
    operator: Mapped[str] = mapped_column(String(100))
    reason: Mapped[str] = mapped_column(Text)
    liquidate: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    state: Mapped[str] = mapped_column(String(20))
    channel: Mapped[str | None] = mapped_column(String(10))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class BacktestJobRow(Base):
    """Backtests pedidos pelo painel; executados em subprocesso, com progresso e resultado gravados aqui."""

    __tablename__ = "backtest_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')",
            name="ck_backtest_jobs_status",
        ),
        Index("ix_backtest_jobs_created_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    created_by: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(10))
    params: Mapped[dict[str, Any]] = mapped_column(JSONB)
    progress: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


LIVE_PHRASE_CHECK = "confirmed_phrase = 'OPERAR COM DINHEIRO REAL'"


class LiveAuthorizationRow(Base):
    """Autorizacao do Live, append-only: so existe se TODOS os criterios valiam no servidor. Nao inicia nada.

    O hash cobre todos os campos; a revogacao e uma linha em `live_authorization_revocations`.
    """

    __tablename__ = "live_authorizations"
    __table_args__ = (
        CheckConstraint(LIVE_PHRASE_CHECK, name="ck_live_authorizations_phrase"),
        CheckConstraint("expires_at > created_at", name="ck_live_authorizations_expiry"),
        CheckConstraint("initial_capital_quote > 0", name="ck_live_authorizations_capital"),
        CheckConstraint("length(btrim(reason)) >= 3", name="ck_live_authorizations_reason"),
        Index("ix_live_authorizations_created", text("created_at DESC"), "id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    paper_run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id"))
    criteria: Mapped[dict[str, Any]] = mapped_column(JSONB)
    actor: Mapped[str] = mapped_column(String(100))
    reason: Mapped[str] = mapped_column(Text)
    confirmed_phrase: Mapped[str] = mapped_column(String(64))
    initial_capital_quote: Mapped[Decimal] = mapped_column(Numeric(28, 10))
    credential_id: Mapped[str] = mapped_column(String(36))
    key_permissions: Mapped[dict[str, Any]] = mapped_column(JSONB)
    record_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LiveAuthorizationRevocationRow(Base):
    """Revogacao (append-only, uma por autorizacao): o runner live para novas entradas; stops preservados."""

    __tablename__ = "live_authorization_revocations"
    __table_args__ = (
        CheckConstraint("length(btrim(reason)) >= 3", name="ck_live_authorization_revocations_reason"),
        UniqueConstraint("authorization_id", name="uq_live_authorization_revocations_authorization"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    authorization_id: Mapped[str] = mapped_column(ForeignKey("live_authorizations.id"))
    actor: Mapped[str] = mapped_column(String(100))
    reason: Mapped[str] = mapped_column(Text)
    revoked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
