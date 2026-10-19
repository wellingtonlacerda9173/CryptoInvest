from enum import StrEnum

from pydantic import Field

from engine.domain.base import DomainModel
from engine.domain.decision import Decision, DecisionAction
from engine.domain.signal import Signal
from engine.domain.types import UtcDatetime

RISK_SOURCE = "risk"


class RejectionCode(StrEnum):
    ROBOT_HALTED = "robot_halted"
    PAUSED = "paused"
    DAILY_LOSS_PAUSE = "daily_loss_pause"
    DAILY_TARGET_REACHED = "daily_target_reached"
    PAIR_PAUSED = "pair_paused"
    MAX_POSITIONS = "max_positions"
    POSITION_ALREADY_OPEN = "position_already_open"
    ADDING_TO_LOSER = "adding_to_loser"
    COIN_EXPOSURE = "coin_exposure"
    SPREAD_TOO_WIDE = "spread_too_wide"
    LOW_LIQUIDITY = "low_liquidity"
    EVENT_WINDOW = "event_window"
    HISTORY_GAP = "history_gap"
    REWARD_RISK = "reward_risk"
    NET_REWARD_RISK = "net_reward_risk"
    INVALID_EQUITY = "invalid_equity"
    QUANTITY_BELOW_MIN = "quantity_below_min"
    NOTIONAL_BELOW_MIN = "notional_below_min"
    INCONSISTENT_INPUT = "inconsistent_input"
    EVENT_CALENDAR_STALE = "event_calendar_stale"
    ENTRY_PENDING = "entry_pending"
    TOTAL_EXPOSURE = "total_exposure"
    QUOTE_NOT_ALLOWED = "quote_not_allowed"
    ASSET_NOT_ALLOWED = "asset_not_allowed"
    SIGNAL_STALE = "signal_stale"
    QUOTE_STALE = "quote_stale"
    MARKET_INCOHERENT = "market_incoherent"
    PRICE_FILTER = "price_filter"


class Rejection(DomainModel):
    """Motivo estruturado de recusa, pronto para o feed de decisoes."""

    code: RejectionCode
    message: str = Field(min_length=1)

    def to_decision(self, signal: Signal, decided_at: UtcDatetime) -> Decision:
        return Decision(
            decided_at=decided_at,
            symbol=signal.symbol,
            action=DecisionAction.SKIP,
            reason=f"{self.code.value}: {self.message}",
            source=RISK_SOURCE,
            signal=signal,
        )
