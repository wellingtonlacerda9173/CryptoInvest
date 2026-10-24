from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

from engine.application.backtest.settings import BacktestSettings
from engine.application.records import Record as Record
from engine.application.risk import AuditEntry
from engine.domain.trade import ExitReason, Trade


class BacktestInvariantError(RuntimeError):
    """O gateway simulado e a gestao de posicao divergiram: o resultado nao e confiavel."""


class HypotheticalStatus(StrEnum):
    TARGET = "target"
    STOP = "stop"
    TIMEOUT = "timeout"
    NOT_FILLED = "not_filled"
    NOT_SIMULATED = "not_simulated"
    UNRESOLVED = "unresolved"


class HypotheticalOutcome(Record):
    """O que teria acontecido com um sinal nao operado (liquido de taxas e slippage; nao afeta o capital)."""

    status: HypotheticalStatus
    pnl: Decimal
    r_multiple: Decimal
    candles: int


class DecisionAction(StrEnum):
    TRADED = "traded"
    SKIPPED = "skipped"


class DecisionRecord(Record):
    at: datetime
    strategy: str
    symbol: str
    action: DecisionAction
    code: str
    message: str
    confidence: float | None = None
    entry: Decimal | None = None
    stop: Decimal | None = None
    target: Decimal | None = None
    order_id: str | None = None
    hypothetical: HypotheticalOutcome | None = None
    context: dict[str, Any] | None = None


class EquityPoint(Record):
    at: datetime
    equity: Decimal
    realized: Decimal
    in_position: bool


class RoundTrip(Record):
    """Posicao completa (entrada + todas as saidas). `r_multiple` = resultado / risco aprovado pelo gate."""

    order_id: str
    symbol: str
    strategy: str
    opened_at: datetime
    closed_at: datetime
    entry_price: Decimal
    quantity: Decimal
    pnl: Decimal
    fees: Decimal
    risk_amount: Decimal
    r_multiple: Decimal
    exit_reasons: tuple[ExitReason, ...]
    legs: int
    exit_labels: tuple[str, ...] = ()
    closed_intra_candle: bool = False

    @property
    def labels(self) -> tuple[str, ...]:
        return self.exit_labels or tuple(r.value for r in self.exit_reasons)

    @property
    def attributed_at(self) -> datetime:
        """Instante de agrupamento por dia/mes: saida intra-vela pertence a vela que fechou (close - 1us)."""
        return self.closed_at - timedelta(microseconds=1) if self.closed_intra_candle else self.closed_at


class BacktestResult(Record):
    symbol: str
    timeframe: str
    strategies: tuple[str, ...]
    settings: BacktestSettings
    generated_at: datetime
    period_start: datetime
    period_end: datetime
    candles: int
    initial_capital: Decimal
    final_equity: Decimal
    final_realized_equity: Decimal
    round_trips: tuple[RoundTrip, ...]
    trades: tuple[Trade, ...]
    decisions: tuple[DecisionRecord, ...]
    steps_without_signal: int
    equity_curve: tuple[EquityPoint, ...]
    risk_events: tuple[AuditEntry, ...]
    halted: bool
    open_at_end: int
    total_slippage_cost: Decimal
