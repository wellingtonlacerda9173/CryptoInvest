from decimal import Decimal
from typing import Annotated, Self

from pydantic import Field, StrictInt, StringConstraints, model_validator

from engine.domain.base import DomainModel
from engine.domain.rules import DEFAULT_MIN_REWARD_RISK
from engine.domain.types import StrictDecimal

MAX_RISK_PER_TRADE = Decimal("0.01")
MIN_RISK_PER_TRADE = Decimal("0.0001")  # piso pratico: abaixo disso o lote nao cobre nem a taxa
MAX_DAILY_LOSS = Decimal("0.05")
MAX_DRAWDOWN = Decimal("0.05")
MAX_OPEN_POSITIONS = 2
MAX_CONSECUTIVE_LOSSES = 2
MIN_PAIR_PAUSE_HOURS = 24
MAX_TOTAL_EXPOSURE = Decimal("0.8")

Asset = Annotated[str, StringConstraints(pattern=r"^[A-Z0-9]{2,12}$")]


class RiskConfig(DomainModel):
    """Parametros de risco com defaults conservadores e tetos rigidos (aplicados em application/risk).

    `max_entry_fee_multiple` (padrao 1): a taxa real da entrada nao pode passar de multiplo x
    `taker_fee_rate`, para que a perda no stop nao ultrapasse o risco aprovado por tolerancia de taxa.
    Desconto de taxa (ex.: BNB) e folga; taxa real acima da taker configurada gera `EmergencyExit`.
    """

    risk_per_trade: StrictDecimal = Field(
        default=Decimal("0.005"), ge=MIN_RISK_PER_TRADE, le=MAX_RISK_PER_TRADE
    )
    min_reward_risk: StrictDecimal = Field(default=DEFAULT_MIN_REWARD_RISK, ge=DEFAULT_MIN_REWARD_RISK)
    max_open_positions: StrictInt = Field(default=MAX_OPEN_POSITIONS, ge=1, le=MAX_OPEN_POSITIONS)
    max_daily_loss: StrictDecimal = Field(default=Decimal("0.01"), gt=0, le=MAX_DAILY_LOSS)
    max_position_fraction: StrictDecimal = Field(default=Decimal("0.25"), gt=0, le=1)
    daily_profit_target: StrictDecimal = Field(default=Decimal("0.01"), gt=0, le=1)
    max_drawdown: StrictDecimal = Field(default=MAX_DRAWDOWN, gt=0, le=MAX_DRAWDOWN)
    consecutive_loss_limit: StrictInt = Field(default=MAX_CONSECUTIVE_LOSSES, ge=1, le=MAX_CONSECUTIVE_LOSSES)
    pair_pause_hours: StrictInt = Field(default=MIN_PAIR_PAUSE_HOURS, ge=MIN_PAIR_PAUSE_HOURS)
    max_coin_exposure_fraction: StrictDecimal = Field(default=Decimal("0.5"), gt=0, le=1)
    partial_exit_fraction: StrictDecimal = Field(default=Decimal("0.5"), gt=0, lt=1)
    trailing_r: StrictDecimal = Field(default=Decimal(1), gt=0)
    profit_giveback: StrictDecimal = Field(default=Decimal("0.4"), gt=0, lt=1)
    max_spread: StrictDecimal = Field(default=Decimal("0.002"), gt=0, lt=1)
    min_quote_volume_24h: StrictDecimal = Field(default=Decimal(1_000_000), ge=0)
    event_margin_before_minutes: StrictInt = Field(default=30, ge=0)
    event_margin_after_minutes: StrictInt = Field(default=30, ge=0)
    max_total_exposure_fraction: StrictDecimal = Field(default=Decimal("0.5"), gt=0, le=MAX_TOTAL_EXPOSURE)
    taker_fee_rate: StrictDecimal = Field(default=Decimal("0.001"), ge=0, le=Decimal("0.01"))
    quote_asset: Asset = "USDT"
    allowed_assets: frozenset[Asset] = frozenset()
    blocked_assets: frozenset[Asset] = frozenset({"BNB"})
    fee_asset: Asset = "BNB"
    max_entry_fee_multiple: StrictDecimal = Field(default=Decimal(1), ge=1)
    max_signal_age_timeframes: StrictInt = Field(default=2, ge=1, le=5)
    max_entry_deviation: StrictDecimal = Field(default=Decimal("0.005"), gt=0, le=Decimal("0.02"))
    max_quote_age_seconds: StrictInt = Field(default=30, ge=1, le=300)
    min_history_candles: StrictInt = Field(default=20, ge=2)

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        if self.max_daily_loss < self.risk_per_trade:
            raise ValueError("max_daily_loss must be >= risk_per_trade")
        return self
