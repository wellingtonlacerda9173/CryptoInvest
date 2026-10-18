from pydantic import Field

from engine.domain.base import DomainModel
from engine.domain.types import Price, Quantity, Symbol, UtcDatetime, Volume


class BookLevel(DomainModel):
    price: Price
    quantity: Quantity


class OrderBookSnapshot(DomainModel):
    symbol: Symbol
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    received_at: UtcDatetime


class Balance(DomainModel):
    asset: str = Field(min_length=1)
    free: Volume
    locked: Volume
