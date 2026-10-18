from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from engine.domain.order import EntryOrder, OrderType
from engine.domain.risk import RiskConfig
from engine.domain.signal import Signal
from engine.domain.types import Timeframe
from engine.infrastructure.binance.backoff import ExponentialBackoff
from engine.infrastructure.config.logging import _redact, configure_logging

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def order_kwargs(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "client_order_id": "abc",
        "symbol": "BTC/USDT",
        "type": OrderType.LIMIT,
        "quantity": Decimal(1),
        "price": Decimal(100),
        "stop_loss": Decimal(95),
        "take_profit": Decimal(110),
        "created_at": NOW,
    }
    return {**base, **over}


def make_signal(**over: object) -> Signal:
    base: dict[str, object] = {
        "symbol": "BTC/USDT",
        "timeframe": Timeframe.M1,
        "created_at": NOW,
        "strategy": "x",
        "entry": Decimal(100),
        "stop_loss": Decimal(90),
        "take_profit": Decimal(120),
    }
    return Signal(**{**base, **over})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "stop",
    [
        None,
        Decimal(0),
        Decimal(-1),
        Decimal(100),
        Decimal(101),
        Decimal("NaN"),
        Decimal("sNaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
    ],
)
def test_entry_order_rejects_invalid_stop(stop: Decimal | None) -> None:
    with pytest.raises(ValidationError):
        EntryOrder(**order_kwargs(stop_loss=stop))  # type: ignore[arg-type]


def test_entry_order_without_stop_field_is_rejected() -> None:
    kwargs = order_kwargs()
    del kwargs["stop_loss"]

    with pytest.raises(ValidationError):
        EntryOrder(**kwargs)  # type: ignore[arg-type]


def test_entry_order_cannot_be_mutated() -> None:
    order = EntryOrder(**order_kwargs())  # type: ignore[arg-type]

    with pytest.raises(ValidationError):
        order.stop_loss = Decimal(200)  # type: ignore[misc]


@pytest.mark.parametrize("bad", [Decimal("NaN"), Decimal("Infinity"), Decimal("-5")])
def test_signal_rejects_non_finite_or_negative_levels(bad: Decimal) -> None:
    with pytest.raises(ValidationError):
        make_signal(stop_loss=bad)


def test_signal_rejects_reward_risk_below_default_minimum() -> None:
    with pytest.raises(ValidationError, match="below minimum"):
        make_signal(take_profit=Decimal("109.99"))


def test_signal_cannot_lower_minimum_reward_risk_below_two() -> None:
    with pytest.raises(ValidationError):
        make_signal(take_profit=Decimal(110), min_reward_risk=Decimal(1))


def test_risk_config_cannot_lower_minimum_reward_risk_below_two() -> None:
    with pytest.raises(ValidationError):
        RiskConfig(min_reward_risk=Decimal(1))


def test_entry_order_enforces_minimum_reward_risk() -> None:
    with pytest.raises(ValidationError):
        EntryOrder(**order_kwargs(take_profit=Decimal("100.01")))  # type: ignore[arg-type]


@pytest.mark.parametrize("stop", [None, Decimal(200)])
def test_entry_order_model_copy_cannot_bypass_stop_validation(stop: Decimal | None) -> None:
    order = EntryOrder(**order_kwargs())  # type: ignore[arg-type]

    with pytest.raises((ValidationError, ValueError, TypeError)):
        order.model_copy(update={"stop_loss": stop})


def test_entry_order_model_construct_cannot_omit_stop() -> None:
    kwargs = order_kwargs()
    del kwargs["stop_loss"]

    with pytest.raises((ValidationError, ValueError, TypeError)):
        EntryOrder.model_construct(**kwargs)


def test_risk_config_model_copy_cannot_exceed_one_percent() -> None:
    with pytest.raises((ValidationError, ValueError, TypeError)):
        RiskConfig().model_copy(update={"risk_per_trade": Decimal("0.5")})


def test_risk_config_defaults_and_ceiling() -> None:
    assert RiskConfig().risk_per_trade == Decimal("0.005")
    assert RiskConfig(risk_per_trade=Decimal("0.01")).risk_per_trade == Decimal("0.01")
    with pytest.raises(ValidationError):
        RiskConfig(risk_per_trade=Decimal("0.0100000000000000000001"))


def test_backoff_never_overflows_during_long_outage() -> None:
    backoff = ExponentialBackoff(base=1, cap=60)

    delays = [backoff.next_delay() for _ in range(1100)]

    assert max(delays) <= 60


@pytest.mark.parametrize(
    "event", [{"x-mbx-apikey": "SECRET-VALUE"}, {"headers": {"X-MBX-APIKEY": "SECRET-VALUE"}}]
)
def test_redaction_covers_binance_key_header_and_nested(event: dict[str, object]) -> None:
    out = _redact(None, "info", dict(event))

    assert "SECRET-VALUE" not in str(out)


def test_configure_logging_accepts_lowercase_level() -> None:
    configure_logging("info")
