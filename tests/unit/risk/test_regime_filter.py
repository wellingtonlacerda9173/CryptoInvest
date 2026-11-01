"""Filtro de regime do gate: so entra em alta confirmada; queda, lateral e duvida recusam."""

import inspect
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from engine.application.risk import (
    ApprovalError,
    ApprovedEntry,
    Rejection,
    RejectionCode,
    RiskState,
    assess_regime,
    evaluate_entry,
    gate,
    manage_position,
    regime_filter,
)
from engine.application.risk.regime_filter import MIN_CANDLES, REGIME_WINDOW, RegimeVerdict
from engine.application.trading_config.risk_delta import Effect, risk_effect
from engine.domain.candle import Candle
from engine.domain.risk import RiskConfig
from tests.unit.risk.factories import EMPTY_CALENDAR, NOW, D, candle, history, market, position, rules, signal

CFG = RiskConfig(taker_fee_rate=D(0))


def _shifted(c: Candle, delta: Decimal) -> Candle:
    return c.model_copy(
        update={
            "open": c.open + delta,
            "high": c.high + delta,
            "low": c.low + delta,
            "close": c.close + delta,
        }
    )


def run(candles: list[Candle], config: RiskConfig = CFG) -> ApprovedEntry | Rejection:
    return evaluate_entry(
        signal(),
        state=RiskState.initial(D(10_000), NOW),
        config=config,
        rules=rules(),
        positions=[],
        pending=[],
        market=market(),
        history=candles,
        calendar=EMPTY_CALENDAR,
        now=NOW,
        client_order_id="e-1",
    )


def test_uptrend_is_approved_and_the_seal_carries_the_evidence() -> None:
    result = run(history(drift=0.2))

    assert isinstance(result, ApprovedEntry)
    assert result.regime.verdict is RegimeVerdict.UPTREND
    assert result.verify() is result


@pytest.mark.parametrize(
    ("drift", "code"),
    [
        (-0.2, RejectionCode.REGIME_DOWNTREND),
        (-0.5, RejectionCode.REGIME_DOWNTREND),
        (0.0, RejectionCode.REGIME_SIDEWAYS),
        (0.05, RejectionCode.REGIME_UNKNOWN),
    ],
)
def test_downtrend_sideways_and_doubt_are_refused(drift: float, code: RejectionCode) -> None:
    result = run(history(drift=drift))

    assert isinstance(result, Rejection)
    assert result.code is code
    assert result.regime is not None
    assert not result.regime.allows_entry


def test_insufficient_history_refuses_as_unknown() -> None:
    result = run(history(MIN_CANDLES - 1))

    assert isinstance(result, Rejection)
    assert result.code == RejectionCode.REGIME_UNKNOWN


def test_open_candle_in_history_refuses() -> None:
    candles = history()
    candles[-1] = candles[-1].model_copy(update={"closed": False})

    assert assess_regime(candles, CFG).verdict is RegimeVerdict.UNKNOWN


def test_assessment_is_deterministic() -> None:
    candles = history(drift=0.2, count=200)

    first = assess_regime(candles, CFG)

    assert assess_regime(list(candles), CFG) == first
    assert assess_regime(candles[:170], CFG).at == candles[169].close_time
    assert assess_regime(candles, CFG) == first


@pytest.mark.parametrize("drift", [0.1, -0.05, 0.0])
def test_verdict_does_not_depend_on_history_beyond_the_fixed_window(drift: float) -> None:
    candles = history(drift=drift, count=REGIME_WINDOW * 2)

    reference = assess_regime(candles[-REGIME_WINDOW:], CFG)

    for size in (REGIME_WINDOW + 1, REGIME_WINDOW + 150, REGIME_WINDOW * 2):
        assert assess_regime(candles[-size:], CFG) == reference


@pytest.mark.parametrize("start", range(4))
def test_minimum_history_always_has_enough_complete_higher_timeframe_groups(start: int) -> None:
    candles = history(drift=0.2, count=MIN_CANDLES + 8)[start : start + MIN_CANDLES]

    assert len(candles) == MIN_CANDLES
    assert assess_regime(candles, CFG).htf_up is not None


def test_same_input_is_assessed_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    original = regime_filter._assess
    monkeypatch.setattr(regime_filter, "_assess", lambda *a: calls.append(1) or original(*a))
    candles = history(drift=0.2, count=180)

    first = assess_regime(candles, CFG)
    assert assess_regime(list(candles), CFG) is first
    assert calls == [1]

    changed = [*candles[:-1], _shifted(candles[-1], Decimal(3))]
    assert assess_regime(changed, CFG) is not first
    stricter = RiskConfig(taker_fee_rate=D(0), regime_rise_min_atr=D(20))
    assess_regime(candles, stricter)
    assert len(calls) == 3


def test_gate_takes_no_regime_from_the_caller() -> None:
    assert "regime" not in inspect.signature(evaluate_entry).parameters


def test_stricter_threshold_turns_a_marginal_uptrend_into_refusal() -> None:
    candles = history(drift=0.2)
    strict = RiskConfig(taker_fee_rate=D(0), regime_rise_min_atr=D(20))

    assert isinstance(run(candles), ApprovedEntry)
    result = run(candles, strict)
    assert isinstance(result, Rejection)
    assert result.code is RejectionCode.REGIME_UNKNOWN


@pytest.mark.parametrize(
    "field", ["regime_adx_min", "regime_er_min", "regime_slope_min_atr", "regime_rise_min_atr"]
)
def test_config_cannot_go_below_the_floor(field: str) -> None:
    default = getattr(RiskConfig(), field)

    with pytest.raises(ValidationError):
        RiskConfig(**{field: default / 100})
    with pytest.raises(ValidationError):
        RiskConfig(**{field: Decimal(0)})


def test_there_is_no_switch_to_disable_the_filter() -> None:
    assert not [name for name in RiskConfig.model_fields if "regime" in name and "enab" in name]
    off = RiskConfig.model_validate({"regime_enabled": False, "taker_fee_rate": "0"})
    assert isinstance(run(history(drift=-0.3), off), Rejection)
    assert isinstance(run(history(drift=0.0), off), Rejection)


@pytest.mark.parametrize(
    "field", ["regime_adx_min", "regime_er_min", "regime_slope_min_atr", "regime_rise_min_atr"]
)
def test_loosening_is_an_increase_of_risk_and_tightening_a_decrease(field: str) -> None:
    value = getattr(RiskConfig(), field)

    assert risk_effect(field, value, value / 2) is Effect.INCREASE
    assert risk_effect(field, value, value * 2) is Effect.DECREASE


def test_approval_cannot_be_forged_without_uptrend_evidence() -> None:
    approved = run(history(drift=0.2))
    assert isinstance(approved, ApprovedEntry)
    sideways = assess_regime(history(drift=0.0), CFG)

    with pytest.raises(TypeError):
        ApprovedEntry(approved.order, approved.signal, D(1), guards=approved.guards, regime=sideways)
    forged = object.__new__(ApprovedEntry)
    with pytest.raises(ApprovalError):
        forged.verify()


def test_exits_and_open_positions_are_not_affected_by_a_downtrend(monkeypatch: pytest.MonkeyPatch) -> None:
    bar = candle("99", "100", "94", "95")
    baseline = manage_position(position(), bar, rules(), RiskConfig())
    assert baseline.exits

    def forbidden(*_a: object, **_k: object) -> None:
        raise AssertionError("a gestao de posicao nao pode consultar o regime")

    monkeypatch.setattr(regime_filter, "_assess", forbidden)
    monkeypatch.setattr(regime_filter, "assess_regime", forbidden)
    monkeypatch.setattr(gate, "assess_regime", forbidden)
    during_downtrend = manage_position(position(), bar, rules(), RiskConfig())

    assert during_downtrend == baseline
    assert [e.reason for e in during_downtrend.exits] == [e.reason for e in baseline.exits]
    monkeypatch.undo()
    assert assess_regime(history(drift=-0.3), CFG).verdict is RegimeVerdict.DOWNTREND
    assert isinstance(run(history(drift=-0.3)), Rejection)


@settings(max_examples=40, deadline=None)
@given(drift=st.floats(min_value=-0.6, max_value=0.0), noise=st.floats(min_value=0.0, max_value=3.0))
def test_never_approves_in_downtrend_or_flat_market(drift: float, noise: float) -> None:
    candles = [
        _shifted(c, Decimal(str(round(noise * (-1) ** i, 3)))) for i, c in enumerate(history(drift=drift))
    ]

    assert isinstance(run(candles), Rejection)


@settings(max_examples=30, deadline=None)
@given(count=st.integers(min_value=0, max_value=MIN_CANDLES - 1))
def test_short_histories_never_pass(count: int) -> None:
    assert not assess_regime(history(count), CFG).allows_entry
