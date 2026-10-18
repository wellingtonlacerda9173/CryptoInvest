from decimal import Decimal
from typing import Self

from pydantic import Field, StrictInt, model_validator

from engine.domain.base import DomainModel
from engine.domain.rules import DEFAULT_MIN_REWARD_RISK
from engine.domain.types import StrictDecimal

MAX_RISK_PER_TRADE = Decimal("0.01")
MAX_DAILY_LOSS = Decimal("0.05")


class RiskConfig(DomainModel):
    """Parametros de risco com defaults conservadores e tetos rigidos. Gestao completa: fase 2."""

    risk_per_trade: StrictDecimal = Field(default=Decimal("0.005"), gt=0, le=MAX_RISK_PER_TRADE)
    min_reward_risk: StrictDecimal = Field(default=DEFAULT_MIN_REWARD_RISK, ge=DEFAULT_MIN_REWARD_RISK)
    max_open_positions: StrictInt = Field(default=3, ge=1, le=20)
    max_daily_loss: StrictDecimal = Field(default=Decimal("0.02"), gt=0, le=MAX_DAILY_LOSS)
    max_position_fraction: StrictDecimal = Field(default=Decimal("0.25"), gt=0, le=1)

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        if self.max_daily_loss < self.risk_per_trade:
            raise ValueError("max_daily_loss must be >= risk_per_trade")
        return self
