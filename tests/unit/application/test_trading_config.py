"""Configuracao versionada: hash estavel, tetos rigidos, aumento de risco e efeito no (re)inicio."""

from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from engine.application.strategies import ElephantBarBreakout, EmaCrossTrend
from engine.application.trading_config import (
    ConfigDocument,
    Effect,
    default_document,
    describe,
    diff_documents,
    effective_document,
    limit_to_reductions,
    risk_effect,
    risk_patch,
)
from engine.application.trading_config.builders import build_strategies_from_config
from engine.domain.risk import RiskConfig

D = Decimal


def with_risk(base: ConfigDocument | None = None, **risk: Any) -> ConfigDocument:
    doc = base or default_document()
    return doc.with_section("risk", {**doc.dump()["risk"], **risk})


def test_digest_is_deterministic_and_ignores_set_order() -> None:
    a = with_risk(allowed_assets=["BTC", "ETH"])
    b = with_risk(allowed_assets=["ETH", "BTC"])
    assert a.digest() == b.digest() and len(a.digest()) == 64
    assert default_document().digest() != a.digest()
    assert ConfigDocument.model_validate(a.dump()).digest() == a.digest()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("risk_per_trade", "0.011"),
        ("max_daily_loss", "0.06"),
        ("max_drawdown", "0.06"),
        ("max_open_positions", 3),
        ("consecutive_loss_limit", 3),
        ("pair_pause_hours", 23),
        ("max_total_exposure_fraction", "0.81"),
        ("min_reward_risk", "1.5"),
    ],
)
def test_hard_ceilings_are_rejected_by_the_document(field: str, value: Any) -> None:
    with pytest.raises(ValidationError):
        with_risk(**{field: value})


def test_values_at_the_ceiling_are_accepted() -> None:
    doc = with_risk(risk_per_trade="0.01", max_daily_loss="0.05", max_drawdown="0.05", max_open_positions=2)
    assert doc.risk.risk_per_trade == D("0.01") and doc.risk.max_open_positions == 2


def test_a_daily_loss_below_the_per_trade_risk_is_invalid() -> None:
    with pytest.raises(ValidationError):
        with_risk(risk_per_trade="0.01", max_daily_loss="0.005")


def test_at_least_one_strategy_and_unique_pairs_are_required() -> None:
    doc = default_document()
    with pytest.raises(ValidationError):
        doc.with_section(
            "strategies", {**doc.dump()["strategies"], "elephant_enabled": False, "ema_enabled": False}
        )
    with pytest.raises(ValidationError):
        doc.with_section("pairs", {"symbols": ["BTC/USDT", "BTC/USDT"], "timeframe": "1h"})


@pytest.mark.parametrize(
    ("name", "before", "after", "effect"),
    [
        ("risk_per_trade", "0.005", "0.006", Effect.INCREASE),
        ("risk_per_trade", "0.005", "0.004", Effect.DECREASE),
        ("max_daily_loss", "0.01", "0.02", Effect.INCREASE),
        ("max_open_positions", 1, 2, Effect.INCREASE),
        ("min_reward_risk", "2", "3", Effect.DECREASE),
        ("min_reward_risk", "3", "2", Effect.INCREASE),
        ("pair_pause_hours", 24, 48, Effect.DECREASE),
        ("max_spread", "0.002", "0.003", Effect.INCREASE),
        ("min_quote_volume_24h", "1000000", "500000", Effect.INCREASE),
        ("blocked_assets", ["BNB"], [], Effect.INCREASE),
        ("blocked_assets", [], ["BNB"], Effect.DECREASE),
        ("allowed_assets", ["BTC"], ["BTC", "ETH"], Effect.INCREASE),
        ("allowed_assets", ["BTC", "ETH"], ["BTC"], Effect.DECREASE),
        ("allowed_assets", ["BTC"], [], Effect.INCREASE),
        ("allowed_assets", [], ["BTC"], Effect.DECREASE),
        ("trailing_r", "1", "2", Effect.UNKNOWN),
        ("taker_fee_rate", "0.001", "0.002", Effect.UNKNOWN),
        ("risk_per_trade", "0.005", "0.0050", Effect.NEUTRAL),
    ],
)
def test_risk_effect_by_field(name: str, before: Any, after: Any, effect: Effect) -> None:
    assert risk_effect(name, before, after) is effect


def test_unknown_effects_count_as_risk_increase_so_they_wait_for_a_restart() -> None:
    changes = diff_documents(default_document(), with_risk(trailing_r="2"))
    [change] = changes
    assert change.effect is Effect.UNKNOWN and change.increases_risk


def test_diff_lists_every_changed_field_with_plain_text_and_cost_per_loss() -> None:
    old = default_document()
    new = with_risk(old, risk_per_trade="0.008", max_open_positions=1)
    changes = {c.path: c for c in diff_documents(old, new)}
    assert set(changes) == {"risk.risk_per_trade", "risk.max_open_positions"}
    text = describe(changes["risk.risk_per_trade"], D(10_000))
    assert text["label"] == "Risco por operação"
    assert text["before_text"] == "0,50%" and text["after_text"] == "0,80%"
    assert "80,00 USDT" in text["cost_text"] and "50,00 USDT" in text["cost_text"]
    assert changes["risk.max_open_positions"].effect is Effect.DECREASE


def test_nested_strategy_changes_are_diffed_and_never_hot_applied() -> None:
    old = default_document()
    strategies = old.dump()["strategies"]
    strategies["elephant"]["rsi_min"] = 40.0
    new = old.with_section("strategies", strategies)
    [change] = diff_documents(old, new)
    assert change.path == "strategies.elephant.rsi_min" and change.section == "strategies"
    assert risk_patch([change], new) == {}


def test_risk_patch_contains_only_reductions() -> None:
    old = default_document()
    new = with_risk(old, risk_per_trade="0.003", max_daily_loss="0.02", min_quote_volume_24h="2000000")
    patch = risk_patch(diff_documents(old, new), new)
    assert patch == {"risk_per_trade": "0.003", "min_quote_volume_24h": "2000000"}


def test_limit_to_reductions_keeps_increases_out_and_takes_reductions() -> None:
    base = RiskConfig()
    wanted = RiskConfig(risk_per_trade=D("0.008"), max_daily_loss=D("0.04"), max_open_positions=1)
    limited = limit_to_reductions(base, wanted)
    assert limited.risk_per_trade == base.risk_per_trade and limited.max_daily_loss == base.max_daily_loss
    assert limited.max_open_positions == 1
    assert limit_to_reductions(base, base) == base


def test_effective_document_without_positions_takes_the_latest_version_whole() -> None:
    running = default_document()
    latest = with_risk(running, risk_per_trade="0.009")
    assert effective_document(latest, running, open_positions=False) is latest
    assert effective_document(latest, None, open_positions=True) is latest


def test_effective_document_with_open_positions_blocks_increases_but_takes_reductions() -> None:
    running = default_document()
    latest = with_risk(running, risk_per_trade="0.009", max_open_positions=1)
    pairs = latest.dump()["pairs"] | {"symbols": ["BTC/USDT", "ETH/USDT", "SOL/USDT"]}
    latest = latest.with_section("pairs", pairs).with_section(
        "ai", {"enabled": True, "model": "claude-sonnet-5-5"}
    )
    effective = effective_document(latest, running, open_positions=True)
    assert effective.risk.risk_per_trade == running.risk.risk_per_trade
    assert effective.risk.max_open_positions == 1
    assert effective.pairs == running.pairs and effective.strategies == running.strategies
    assert effective.ai.enabled is True


def test_strategies_are_built_from_the_document_with_all_parameters() -> None:
    doc = default_document()
    data = doc.dump()["strategies"]
    data["ema_enabled"] = False
    data["elephant"]["rsi_min"] = 42.0
    data["min_confidence"] = 0.7
    only_elephant = build_strategies_from_config(doc.with_section("strategies", data).strategies)
    assert len(only_elephant) == 1 and isinstance(only_elephant[0], ElephantBarBreakout)
    both = build_strategies_from_config(doc.strategies)
    assert [type(s) for s in both] == [ElephantBarBreakout, EmaCrossTrend]
