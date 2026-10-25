from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from engine.application.live_authorization import (
    LIVE_PHRASE,
    ActivationInputs,
    ActivationPolicy,
    KeyFacts,
    LiveStage,
    authorization_hash,
    authorization_mac,
    authorization_mac_valid,
    authorization_record,
    evaluate_activation,
    live_stage,
)
from engine.application.paper_metrics.metrics import PaperMetrics

D = Decimal
NOW = datetime(2026, 5, 1, tzinfo=UTC)
POLICY = ActivationPolicy(
    initial_capital_cap=D(100),
    require_ip_restriction=True,
    block_on_key_warnings=True,
    key_max_age=timedelta(hours=24),
    ttl=timedelta(hours=24),
)


def metrics(**overrides: object) -> PaperMetrics:
    values: dict[str, object] = {
        "operating_days": 60,
        "trades": 30,
        "losing_trades": 5,
        "total_pnl": D(500),
        "gross_profit": D(900),
        "gross_loss": D(400),
        "max_drawdown": D("0.049"),
        "profit_factor": D("1.51"),
        "payoff": D(2),
        "worst_daily_loss": D(100),
        "worst_daily_loss_pct": D("0.01"),
    }
    return PaperMetrics.model_validate(values | overrides)


def key(**overrides: object) -> KeyFacts:
    base = KeyFacts(
        can_read=True,
        can_trade=True,
        can_withdraw=False,
        can_transfer=False,
        margin_enabled=False,
        futures_enabled=False,
        ip_restricted=True,
        verified_at=NOW - timedelta(minutes=1),
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def inputs(**overrides: object) -> ActivationInputs:
    base = ActivationInputs(
        now=NOW,
        run_id="paper-1",
        no_test=False,
        eligible=True,
        gate_approved=True,
        metrics=metrics(),
        daily_loss_limit=D("0.01"),
        phrase=LIVE_PHRASE,
        confirm_real_money=True,
        initial_capital=D(100),
        key=key(),
        live_run_active=False,
        active_authorization=False,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def unmet(**overrides: object) -> set[str]:
    return {u.code for u in evaluate_activation(inputs(**overrides), POLICY)}


def test_boundary_values_that_exactly_meet_every_criterion_pass() -> None:
    assert unmet() == set()


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"metrics": metrics(operating_days=59)}, "operating_days"),
        ({"metrics": metrics(max_drawdown=D("0.05"))}, "max_drawdown"),
        ({"metrics": metrics(max_drawdown=None)}, "max_drawdown"),
        ({"metrics": metrics(profit_factor=D("1.5"))}, "profit_factor"),
        ({"metrics": metrics(profit_factor=None)}, "profit_factor"),
        ({"metrics": metrics(trades=29)}, "min_trades"),
        ({"metrics": metrics(losing_trades=4)}, "min_losses"),
        ({"metrics": metrics(worst_daily_loss_pct=D("0.0101"))}, "daily_loss"),
        ({"eligible": False}, "ineligible_run"),
        ({"gate_approved": False}, "gate_not_approved"),
        ({"no_test": True}, "no_test"),
        ({"run_id": None}, "no_test"),
        ({"phrase": "operar com dinheiro real"}, "phrase"),
        ({"phrase": LIVE_PHRASE + " "}, "phrase"),
        ({"phrase": None}, "phrase"),
        ({"confirm_real_money": False}, "confirm_real_money"),
        ({"initial_capital": None}, "initial_capital"),
        ({"initial_capital": D(0)}, "initial_capital"),
        ({"initial_capital": D("-1")}, "initial_capital"),
        ({"initial_capital": D("NaN")}, "initial_capital"),
        ({"initial_capital": D("100.0001")}, "initial_capital_above_cap"),
        ({"key": None}, "key_missing"),
        ({"key": key(verified_at=None)}, "key_unverified"),
        ({"key": key(verified_at=NOW - timedelta(hours=25))}, "key_unverified"),
        ({"key": key(verified_at=NOW + timedelta(minutes=1))}, "key_unverified"),
        ({"key": key(can_withdraw=True)}, "key_withdraw"),
        ({"key": key(can_trade=False)}, "key_cannot_trade"),
        ({"key": key(can_read=False)}, "key_cannot_trade"),
        ({"key": key(ip_restricted=False)}, "key_ip_unrestricted"),
        ({"key": key(ip_restricted=None)}, "key_ip_unrestricted"),
        ({"key": key(can_transfer=True)}, "key_warnings"),
        ({"key": key(margin_enabled=True)}, "key_warnings"),
        ({"key": key(futures_enabled=True)}, "key_warnings"),
        ({"live_run_active": True}, "live_run_active"),
        ({"active_authorization": True}, "authorization_active"),
    ],
)
def test_each_condition_alone_blocks(overrides: dict[str, object], code: str) -> None:
    assert unmet(**overrides) == {code}


def test_weaker_policy_never_lowers_the_hard_floors() -> None:
    weak = replace(POLICY, require_ip_restriction=False, block_on_key_warnings=False)

    assert not evaluate_activation(inputs(key=key(ip_restricted=None, margin_enabled=True)), weak)
    assert {u.code for u in evaluate_activation(inputs(metrics=metrics(operating_days=1)), weak)} == {
        "operating_days"
    }


def test_every_unmet_item_carries_its_reason() -> None:
    result = evaluate_activation(inputs(metrics=metrics(operating_days=3), phrase="x"), POLICY)

    assert {u.code for u in result} == {"operating_days", "phrase"}
    assert all(u.message for u in result)


def test_hash_is_stable_and_sensitive() -> None:
    record = {"id": "a", "n": D("1.50"), "when": NOW}

    assert authorization_hash(record) == authorization_hash(dict(reversed(record.items())))
    assert authorization_hash(record) != authorization_hash(record | {"id": "b"})
    assert len(authorization_hash(record)) == 64


@pytest.mark.parametrize(
    ("days", "stage", "allowed"),
    [
        (0, LiveStage.REDUCED_CAPITAL, False),
        (6, LiveStage.REDUCED_CAPITAL, False),
        (7, LiveStage.OBSERVATION, False),
        (29, LiveStage.OBSERVATION, False),
        (30, LiveStage.LIMIT_INCREASE_AVAILABLE, True),
        (90, LiveStage.LIMIT_INCREASE_AVAILABLE, True),
    ],
)
def test_limit_increase_only_after_thirty_days_within_goals(
    days: int, stage: LiveStage, allowed: bool
) -> None:
    state = live_stage(
        authorized=True,
        live_started_at=NOW - timedelta(days=days),
        live_metrics=metrics(),
        daily_loss_limit=D("0.01"),
        now=NOW,
    )

    assert (state.stage, state.limit_increase_allowed) == (stage, allowed)


@pytest.mark.parametrize(
    "bad",
    [
        metrics(max_drawdown=D("0.05")),
        metrics(max_drawdown=None),
        metrics(worst_daily_loss_pct=D("0.02")),
        metrics(total_pnl=D("-1")),
    ],
)
def test_thirty_days_outside_goals_blocks_limit_increase(bad: PaperMetrics) -> None:
    state = live_stage(
        authorized=True,
        live_started_at=NOW - timedelta(days=45),
        live_metrics=bad,
        daily_loss_limit=D("0.01"),
        now=NOW,
    )

    assert state.stage is LiveStage.LIMIT_INCREASE_BLOCKED and not state.limit_increase_allowed


def test_no_authorization_or_not_started_never_allows_increase() -> None:
    off = live_stage(
        authorized=False, live_started_at=None, live_metrics=None, daily_loss_limit=D("0.01"), now=NOW
    )
    idle = live_stage(
        authorized=True, live_started_at=None, live_metrics=None, daily_loss_limit=D("0.01"), now=NOW
    )

    assert (off.stage, idle.stage) == (LiveStage.NO_AUTHORIZATION, LiveStage.AUTHORIZED_WAITING_START)
    assert not (off.limit_increase_allowed or idle.limit_increase_allowed)


def test_a_loose_paper_daily_loss_threshold_never_loosens_the_fixed_one_percent_floor() -> None:
    lenient = inputs(metrics=metrics(worst_daily_loss_pct=D("0.03")), daily_loss_limit=D("0.05"))

    assert "daily_loss" in {u.code for u in evaluate_activation(lenient, POLICY)}


def test_a_stricter_run_threshold_still_applies_below_the_floor() -> None:
    strict = inputs(metrics=metrics(worst_daily_loss_pct=D("0.006")), daily_loss_limit=D("0.005"))

    assert "daily_loss" in {u.code for u in evaluate_activation(strict, POLICY)}


def test_limit_increase_is_blocked_by_the_floor_even_with_a_loose_run_threshold() -> None:
    state = live_stage(
        authorized=True,
        live_started_at=NOW - timedelta(days=40),
        live_metrics=metrics(worst_daily_loss_pct=D("0.03")),
        daily_loss_limit=D("0.05"),
        now=NOW,
    )

    assert state.stage is LiveStage.LIMIT_INCREASE_BLOCKED


MAC_KEY = b"k" * 48


def mac_record(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "authorization_id": "a" * 32,
        "paper_run_id": "paper-1",
        "criteria": {"gate": [1, 2], "metrics": {"pf": "1.6"}},
        "actor": "admin",
        "reason": "ativar",
        "confirmed_phrase": LIVE_PHRASE,
        "initial_capital_quote": D("100.0000000000"),
        "credential_id": "cred-1",
        "key_permissions": {"can_trade": True},
        "created_at": NOW,
        "expires_at": NOW + timedelta(hours=24),
    }
    return authorization_record(**(base | overrides))  # type: ignore[arg-type]


def test_authorization_mac_is_a_keyed_hmac_not_a_plain_sha256() -> None:
    record = mac_record()

    mac = authorization_mac(MAC_KEY, record)

    assert len(mac) == 64 and mac != authorization_hash(record)
    assert mac != authorization_mac(b"x" * 48, record)


def test_authorization_mac_survives_the_database_roundtrip_of_decimals_and_timezones() -> None:
    stored = authorization_mac(MAC_KEY, mac_record())
    reloaded = mac_record(initial_capital_quote=D("100"), created_at=NOW.astimezone(UTC))

    assert authorization_mac_valid(MAC_KEY, reloaded, stored)


@pytest.mark.parametrize(
    "tamper",
    [
        {"initial_capital_quote": D("100.01")},
        {"credential_id": "cred-2"},
        {"actor": "intruso"},
        {"expires_at": NOW + timedelta(days=365)},
        {"criteria": {"gate": [1, 2, 3], "metrics": {"pf": "1.6"}}},
    ],
)
def test_the_consumer_rejects_a_tampered_record(tamper: dict[str, object]) -> None:
    stored = authorization_mac(MAC_KEY, mac_record())

    assert not authorization_mac_valid(MAC_KEY, mac_record(**tamper), stored)


def test_the_consumer_rejects_a_wrong_key_a_garbage_mac_and_a_short_key() -> None:
    record = mac_record()
    stored = authorization_mac(MAC_KEY, record)

    assert not authorization_mac_valid(b"z" * 48, record, stored)
    assert not authorization_mac_valid(MAC_KEY, record, "0" * 64)
    assert not authorization_mac_valid(MAC_KEY, record, None)
    assert not authorization_mac_valid(MAC_KEY, record, "é" * 64)
    with pytest.raises(ValueError, match="curta"):
        authorization_mac(b"short", record)
