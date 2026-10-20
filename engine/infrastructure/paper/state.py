from typing import Literal

from pydantic import Field

from engine.domain.base import DomainModel
from engine.domain.order import Order, OrderStatus
from engine.domain.types import Fee, Price, StrictDecimal, Symbol, UtcDatetime, Volume
from engine.infrastructure.backtest.fills import Fill
from engine.infrastructure.paper.config import PaperConfig


class PaperOrderState(DomainModel):
    order: Order
    order_id: str
    status: OrderStatus
    filled_quantity: Volume
    average_price: Price | None
    reserved_quote: Volume
    rejection: str | None
    marketable: bool = False
    submitted_at: UtcDatetime
    executable_at: UtcDatetime


class PaperOcoState(DomainModel):
    entry_client_order_id: str
    order_id: str
    symbol: Symbol
    quantity: Volume
    stop: Price
    target: Price | None
    armed_at: UtcDatetime
    status: OrderStatus
    filled_quantity: Volume
    average_price: Price | None


class PaperPosition(DomainModel):
    """Posicao spot long. `average_price` exclui taxas; `realized_pnl` e bruto de taxas (`fees_paid`)."""

    symbol: Symbol
    quantity: Volume
    average_price: Volume
    opened_at: UtcDatetime | None
    realized_pnl: StrictDecimal
    fees_paid: Fee


class PaperState(DomainModel):
    """Estado do gateway (completo ou compacto); `model_dump(mode="json")` e JSON puro e restauravel."""

    version: Literal[1] = 1
    config: PaperConfig
    balances: dict[str, Volume]
    orders: tuple[PaperOrderState, ...] = ()
    ocos: tuple[PaperOcoState, ...] = ()
    positions: tuple[PaperPosition, ...] = ()
    fills: tuple[Fill, ...] = ()
    fill_seq: int = Field(default=0, ge=0)
    retired_order_ids: tuple[str, ...] = ()
    order_seq: int = Field(default=0, ge=0)
    last_candle_open: dict[str, UtcDatetime] = Field(default_factory=dict)
    last_update_at: UtcDatetime | None = None
