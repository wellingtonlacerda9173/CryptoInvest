from decimal import Decimal
from typing import Self

from pydantic import model_validator

from engine.domain.base import DomainModel
from engine.domain.types import Price, Quantity, Symbol, UtcDatetime


class Position(DomainModel):
    """Posicao long spot aberta; sempre com stop loss abaixo do preco de entrada."""

    symbol: Symbol
    quantity: Quantity
    entry_price: Price
    stop_loss: Price
    take_profit: Price | None = None
    opened_at: UtcDatetime

    @model_validator(mode="after")
    def _check_levels(self) -> Self:
        if not self.stop_loss < self.entry_price:
            raise ValueError("stop_loss must be below entry_price (long only)")
        if self.take_profit is not None and not self.take_profit > self.entry_price:
            raise ValueError("take_profit must be above entry_price (long only)")
        return self

    def unrealized_pnl(self, mark_price: Decimal) -> Decimal:
        return (mark_price - self.entry_price) * self.quantity

    @property
    def initial_risk(self) -> Decimal:
        return (self.entry_price - self.stop_loss) * self.quantity
