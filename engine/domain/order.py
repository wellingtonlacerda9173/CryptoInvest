from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, TypeAdapter, model_validator

from engine.domain.base import DomainModel
from engine.domain.rules import DEFAULT_MIN_REWARD_RISK, meets_reward_risk
from engine.domain.signal import Signal
from engine.domain.types import Price, Quantity, Symbol, UtcDatetime, Volume


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"


class OrderStatus(StrEnum):
    NEW = "new"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"


class EntryOrder(DomainModel):
    """Compra spot. Nao existe sem stop loss: campo obrigatorio e validado contra o preco."""

    side: Literal["buy"] = "buy"
    client_order_id: str = Field(min_length=1, max_length=36)
    symbol: Symbol
    type: OrderType
    quantity: Quantity
    price: Price
    stop_loss: Price
    take_profit: Price
    created_at: UtcDatetime

    @model_validator(mode="after")
    def _check_levels(self) -> Self:
        if not self.stop_loss < self.price:
            raise ValueError("stop_loss must be below entry price (long only)")
        if not self.take_profit > self.price:
            raise ValueError("take_profit must be above entry price (long only)")
        if not meets_reward_risk(self.price, self.stop_loss, self.take_profit, DEFAULT_MIN_REWARD_RISK):
            raise ValueError(f"reward/risk below minimum {DEFAULT_MIN_REWARD_RISK}")
        return self

    @classmethod
    def from_signal(
        cls,
        signal: Signal,
        *,
        client_order_id: str,
        quantity: Decimal,
        order_type: OrderType = OrderType.LIMIT,
    ) -> Self:
        return cls(
            client_order_id=client_order_id,
            symbol=signal.symbol,
            type=order_type,
            quantity=quantity,
            price=signal.entry,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            created_at=signal.created_at,
        )


class ExitOrder(DomainModel):
    """Venda para encerrar posicao long existente (spot nao tem venda a descoberto)."""

    side: Literal["sell"] = "sell"
    client_order_id: str = Field(min_length=1, max_length=36)
    symbol: Symbol
    type: OrderType
    quantity: Quantity
    price: Price | None = None
    created_at: UtcDatetime

    @model_validator(mode="after")
    def _check_price(self) -> Self:
        if self.type is OrderType.LIMIT and self.price is None:
            raise ValueError("limit order requires price")
        return self


Order = Annotated[EntryOrder | ExitOrder, Field(discriminator="side")]

_ORDER_ADAPTER: TypeAdapter[EntryOrder | ExitOrder] = TypeAdapter(Order)


def revalidate_order(order: EntryOrder | ExitOrder) -> EntryOrder | ExitOrder:
    """Fronteira de saida: todo `place_order` deve passar por aqui antes de falar com a corretora."""
    return _ORDER_ADAPTER.validate_python(order.model_dump())


class OrderReport(DomainModel):
    """Estado de uma ordem reportado pela corretora."""

    order_id: str
    client_order_id: str
    symbol: Symbol
    status: OrderStatus
    filled_quantity: Volume
    average_price: Price | None = None
