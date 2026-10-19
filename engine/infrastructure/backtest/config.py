from decimal import Decimal

from pydantic import Field

from engine.domain.base import DomainModel
from engine.domain.types import Fee, StrictDecimal, Volume


class BacktestConfig(DomainModel):
    """Parametros da simulacao. Taxas como fracao (0.001 = 0,1%); slippage em pontos-base."""

    initial_quote: Volume = Decimal(10_000)
    maker_fee: Fee = Decimal("0.001")
    taker_fee: Fee = Decimal("0.001")
    slippage_bps: StrictDecimal = Field(default=Decimal(0), ge=0)
    latency_candles: int = Field(default=1, ge=1)
