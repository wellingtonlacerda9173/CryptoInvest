from decimal import Decimal
from typing import Self

from pydantic import Field, model_validator

from engine.domain.base import DomainModel
from engine.domain.types import Fee, StrictDecimal, Symbol, Volume


class PaperConfig(DomainModel):
    """Premissas da simulacao. Taxas como fracao (0.001 = 0,1%); slippage em pontos-base; latencia em ms."""

    symbols: tuple[Symbol, ...] = Field(default=("BTC/USDT",), min_length=1)
    quote_asset: str = Field(default="USDT", min_length=1)
    initial_quote: Volume = Decimal(10_000)
    maker_fee: Fee = Decimal("0.001")
    taker_fee: Fee = Decimal("0.001")
    slippage_bps: StrictDecimal = Field(default=Decimal(5), ge=0)
    latency_ms: int = Field(default=100, ge=0)
    max_book_age_seconds: int = Field(default=10, gt=0)
    entry_limit_ttl_seconds: int | None = Field(default=None, gt=0)
    max_stop_blind_seconds: int = Field(default=30, gt=0)
    thin_book_slippage_bps: StrictDecimal = Field(default=Decimal(20), ge=0, lt=10_000)
    book_depth: int = Field(default=10, ge=1, le=20)

    @model_validator(mode="after")
    def _single_quote_asset(self) -> Self:
        if len(set(self.symbols)) != len(self.symbols):
            raise ValueError("symbols must be unique")
        if any(symbol.split("/")[1] != self.quote_asset for symbol in self.symbols):
            raise ValueError("all symbols must be quoted in quote_asset")
        return self
