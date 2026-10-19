from decimal import Decimal
from enum import StrEnum
from typing import Literal

from engine.domain.base import DomainModel
from engine.domain.trade import ExitReason
from engine.domain.types import Fee, Price, Quantity, Symbol, UtcDatetime


class Liquidity(StrEnum):
    MAKER = "maker"
    TAKER = "taker"


class FillKind(StrEnum):
    ENTRY_MARKET = "entry_market"
    ENTRY_LIMIT = "entry_limit"
    EXIT_MARKET = "exit_market"
    EXIT_LIMIT = "exit_limit"
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"


class Fill(DomainModel):
    """Execucao simulada. `fee` em moeda de cotacao; `price` ja inclui o slippage."""

    sequence: int
    order_id: str
    client_order_id: str
    symbol: Symbol
    side: Literal["buy", "sell"]
    kind: FillKind
    liquidity: Liquidity
    quantity: Quantity
    price: Price
    fee: Fee
    slippage_cost: Fee
    exit_reason: ExitReason | None = None
    at: UtcDatetime

    @property
    def notional(self) -> Decimal:
        return self.price * self.quantity
