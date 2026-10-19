from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from engine.domain.risk import RiskConfig
from engine.domain.signal import Signal
from engine.domain.types import Timeframe


def signal() -> Signal:
    return Signal(
        symbol="BTC/USDT",
        timeframe=Timeframe.H1,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        strategy="s",
        entry=Decimal(100),
        stop_loss=Decimal(90),
        take_profit=Decimal(120),
    )


def test_model_copy_rejects_unknown_keys() -> None:
    with pytest.raises(ValidationError, match="stop_los"):
        signal().model_copy(update={"stop_los": Decimal(1)})


def test_model_copy_with_known_keys_still_works_and_tracks_fields_set() -> None:
    cfg = RiskConfig()

    copy = cfg.model_copy(update={"max_open_positions": 1})

    assert copy.max_open_positions == 1
    assert copy.model_fields_set == {"max_open_positions"}
    assert cfg.model_fields_set == set()


def test_model_construct_preserves_fields_set() -> None:
    cfg = RiskConfig.model_construct(max_open_positions=1)

    assert cfg.model_fields_set == {"max_open_positions"}


def test_model_construct_honors_explicit_fields_set() -> None:
    cfg = RiskConfig.model_construct({"max_open_positions", "risk_per_trade"}, max_open_positions=1)

    assert cfg.model_fields_set == {"max_open_positions", "risk_per_trade"}


@pytest.mark.parametrize("value", [True, False])
def test_risk_config_rejects_bool_as_int(value: bool) -> None:
    with pytest.raises(ValidationError):
        RiskConfig(max_open_positions=value)


def test_risk_config_copy_rejects_bool_as_int() -> None:
    with pytest.raises(ValidationError):
        RiskConfig().model_copy(update={"max_open_positions": True})
