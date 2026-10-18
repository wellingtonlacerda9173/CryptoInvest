from decimal import Decimal
from typing import Self

from pydantic import Field, model_validator

from engine.domain.base import DomainModel
from engine.domain.rules import DEFAULT_MIN_REWARD_RISK, meets_reward_risk, reward_risk
from engine.domain.types import Price, StrictDecimal, Symbol, Timeframe, UtcDatetime


class Signal(DomainModel):
    """Sinal de entrada long spot. Stop e alvo sao obrigatorios e validados contra a entrada."""

    symbol: Symbol
    timeframe: Timeframe
    created_at: UtcDatetime
    strategy: str = Field(min_length=1)
    entry: Price
    stop_loss: Price
    take_profit: Price
    # piso rigido: o sinal pode exigir mais que 1:2, nunca menos
    min_reward_risk: StrictDecimal = Field(default=DEFAULT_MIN_REWARD_RISK, ge=DEFAULT_MIN_REWARD_RISK)

    @model_validator(mode="after")
    def _check_levels(self) -> Self:
        if not self.stop_loss < self.entry:
            raise ValueError("stop_loss must be below entry (long only)")
        if not self.take_profit > self.entry:
            raise ValueError("take_profit must be above entry (long only)")
        if not meets_reward_risk(self.entry, self.stop_loss, self.take_profit, self.min_reward_risk):
            raise ValueError(f"reward/risk {self.reward_risk:.4f} below minimum {self.min_reward_risk}")
        return self

    @property
    def reward_risk(self) -> Decimal:
        return reward_risk(self.entry, self.stop_loss, self.take_profit)
