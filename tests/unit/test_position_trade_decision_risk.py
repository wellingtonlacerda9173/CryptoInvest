from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from engine.domain.decision import Decision, DecisionAction
from engine.domain.position import Position
from engine.domain.risk import RiskConfig
from engine.domain.signal import Signal
from engine.domain.trade import ExitReason, Trade
from engine.domain.types import Timeframe

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def test_position_stop_must_be_below_entry() -> None:
    with pytest.raises(ValidationError, match="stop_loss"):
        Position(
            symbol="BTC/USDT",
            quantity=Decimal(1),
            entry_price=Decimal(100),
            stop_loss=Decimal(100),
            opened_at=NOW,
        )


def test_position_pnl_and_risk() -> None:
    pos = Position(
        symbol="BTC/USDT", quantity=Decimal(2), entry_price=Decimal(100), stop_loss=Decimal(95), opened_at=NOW
    )
    assert pos.unrealized_pnl(Decimal(110)) == 20
    assert pos.initial_risk == 10


def test_trade_pnl_includes_fees() -> None:
    trade = Trade(
        symbol="BTC/USDT",
        quantity=Decimal(2),
        entry_price=Decimal(100),
        exit_price=Decimal(110),
        entry_fee=Decimal(1),
        exit_fee=Decimal("1.5"),
        opened_at=NOW,
        closed_at=NOW + timedelta(hours=1),
        exit_reason=ExitReason.TAKE_PROFIT,
    )
    assert trade.pnl == Decimal("17.5")


def test_trade_cannot_close_before_open() -> None:
    with pytest.raises(ValidationError, match="closed_at"):
        Trade(
            symbol="BTC/USDT",
            quantity=Decimal(1),
            entry_price=Decimal(1),
            exit_price=Decimal(1),
            opened_at=NOW,
            closed_at=NOW - timedelta(seconds=1),
            exit_reason=ExitReason.MANUAL,
        )


def test_enter_decision_requires_signal() -> None:
    with pytest.raises(ValidationError, match="signal"):
        Decision(decided_at=NOW, symbol="BTC/USDT", action=DecisionAction.ENTER, reason="x", source="t")
    sig = Signal(
        symbol="BTC/USDT",
        timeframe=Timeframe.H1,
        created_at=NOW,
        strategy="t",
        entry=Decimal(100),
        stop_loss=Decimal(95),
        take_profit=Decimal(110),
    )
    assert Decision(
        decided_at=NOW, symbol="BTC/USDT", action=DecisionAction.ENTER, reason="x", source="t", signal=sig
    )


def test_decision_requires_reason() -> None:
    with pytest.raises(ValidationError):
        Decision(decided_at=NOW, symbol="BTC/USDT", action=DecisionAction.HOLD, reason="", source="t")


def test_risk_defaults() -> None:
    cfg = RiskConfig()
    assert cfg.risk_per_trade == Decimal("0.005")
    assert cfg.min_reward_risk == 2


def test_risk_per_trade_ceiling() -> None:
    assert RiskConfig(risk_per_trade=Decimal("0.01")).risk_per_trade == Decimal("0.01")
    with pytest.raises(ValidationError):
        RiskConfig(risk_per_trade=Decimal("0.0101"))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"risk_per_trade": Decimal(0)},
        {"max_daily_loss": Decimal("0.06")},
        {"max_daily_loss": Decimal("0.001")},
    ],
)
def test_risk_invalid_values(kwargs: dict[str, Decimal]) -> None:
    with pytest.raises(ValidationError):
        RiskConfig(**kwargs)  # type: ignore[arg-type]
