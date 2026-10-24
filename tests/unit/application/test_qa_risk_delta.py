"""QA: TODO campo de RiskConfig que relaxa um limite tem de ser lido como aumento de risco (nunca reducao)."""

from decimal import Decimal
from typing import Any

import pytest

from engine.application.trading_config.risk_delta import Effect, risk_effect
from engine.domain.risk import RiskConfig

# (campo, valor atual, valor MAIS arriscado, valor MENOS arriscado)
CASES: list[tuple[str, Any, Any, Any]] = [
    ("risk_per_trade", "0.005", "0.006", "0.004"),
    ("min_reward_risk", "2", "1.5", "3"),
    ("max_open_positions", 1, 2, None),
    ("max_daily_loss", "0.01", "0.02", "0.009"),
    ("max_position_fraction", "0.25", "0.3", "0.2"),
    ("daily_profit_target", "0.01", "0.02", "0.005"),
    ("max_drawdown", "0.03", "0.05", "0.02"),
    ("consecutive_loss_limit", 1, 2, None),
    ("pair_pause_hours", 48, 24, 72),
    ("max_coin_exposure_fraction", "0.5", "0.6", "0.4"),
    ("max_total_exposure_fraction", "0.5", "0.6", "0.4"),
    ("max_spread", "0.002", "0.003", "0.001"),
    ("min_quote_volume_24h", "1000000", "500000", "2000000"),
    ("event_margin_before_minutes", 30, 10, 60),
    ("event_margin_after_minutes", 30, 10, 60),
    ("max_entry_fee_multiple", "1", "2", None),
    ("max_signal_age_timeframes", 2, 3, 1),
    ("max_entry_deviation", "0.005", "0.01", "0.004"),
    ("max_quote_age_seconds", 30, 60, 10),
    ("min_history_candles", 20, 10, 50),
    ("allowed_assets", ["BTC", "ETH"], [], ["BTC"]),
    ("blocked_assets", ["BNB", "DOGE"], ["BNB"], ["BNB", "DOGE", "XRP"]),
]
# campos sem sentido de risco claro: a regra segura e tratar QUALQUER mudanca como aumento (UNKNOWN)
UNJUDGED = [
    "partial_exit_fraction",
    "trailing_r",
    "profit_giveback",
    "taker_fee_rate",
    "quote_asset",
    "fee_asset",
]


def test_every_risk_field_is_either_judged_or_explicitly_unjudged() -> None:
    covered = {c[0] for c in CASES} | set(UNJUDGED)
    assert covered == set(RiskConfig.model_fields)


@pytest.mark.parametrize(("name", "current", "riskier", "safer"), CASES, ids=[c[0] for c in CASES])
def test_relaxing_a_limit_is_an_increase_and_tightening_is_not(
    name: str, current: Any, riskier: Any, safer: Any
) -> None:
    assert risk_effect(name, current, riskier) is Effect.INCREASE
    if safer is not None:
        assert risk_effect(name, current, safer) is Effect.DECREASE


@pytest.mark.parametrize("name", UNJUDGED)
def test_unjudged_fields_never_count_as_a_reduction(name: str) -> None:
    default = RiskConfig().model_dump(mode="json")[name]
    other = "0.7" if isinstance(default, str) and default.replace(".", "").isdigit() else "XYZ"
    assert risk_effect(name, default, other) in (Effect.INCREASE, Effect.UNKNOWN)


def test_decimal_strings_with_trailing_zeros_are_neutral() -> None:
    assert risk_effect("risk_per_trade", "0.0050", Decimal("0.005")) is Effect.NEUTRAL
