from decimal import Decimal
from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from engine.domain.base import DomainModel
from engine.domain.types import Fee, Price, Quantity, Symbol, UtcDatetime


class ExitReason(StrEnum):
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    PROFIT_PROTECTION = "profit_protection"
    SIGNAL = "signal"
    MANUAL = "manual"


class Trade(DomainModel):
    """Operacao encerrada (round-trip). Taxas em moeda de cotacao."""

    trade_id: str | None = Field(default=None, min_length=1)
    symbol: Symbol
    quantity: Quantity
    entry_price: Price
    exit_price: Price
    entry_fee: Fee = Decimal(0)
    exit_fee: Fee = Decimal(0)
    opened_at: UtcDatetime
    closed_at: UtcDatetime
    exit_reason: ExitReason

    @model_validator(mode="after")
    def _check_times(self) -> Self:
        if self.closed_at < self.opened_at:
            raise ValueError("closed_at must not precede opened_at")
        return self

    @property
    def pnl(self) -> Decimal:
        gross = (self.exit_price - self.entry_price) * self.quantity
        return gross - self.entry_fee - self.exit_fee
