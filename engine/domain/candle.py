from datetime import datetime, timedelta
from typing import Self

from pydantic import model_validator

from engine.domain.base import DomainModel
from engine.domain.types import Price, Symbol, Timeframe, UtcDatetime, Volume


class Candle(DomainModel):
    """Vela OHLCV. `closed` indica que o periodo terminou e os valores sao definitivos."""

    symbol: Symbol
    timeframe: Timeframe
    open_time: UtcDatetime
    open: Price
    high: Price
    low: Price
    close: Price
    volume: Volume
    closed: bool

    @model_validator(mode="after")
    def _check_ohlc(self) -> Self:
        if self.high < max(self.open, self.close):
            raise ValueError("high must be >= max(open, close)")
        if self.low > min(self.open, self.close):
            raise ValueError("low must be <= min(open, close)")
        return self

    @property
    def close_time(self) -> datetime:
        return self.open_time + timedelta(seconds=self.timeframe.seconds)
