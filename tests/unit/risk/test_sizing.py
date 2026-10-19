from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from engine.application.risk import PositionSize, Rejection, RejectionCode, size_position
from tests.unit.risk.factories import D, rules

HALF_PERCENT = D("0.005")


def size(
    equity: str = "10000", entry: str = "100", stop: str = "95", **kw: object
) -> PositionSize | Rejection:
    args: dict[str, object] = {
        "equity": D(equity),
        "risk_fraction": HALF_PERCENT,
        "max_position_fraction": D(1),
        "entry": D(entry),
        "stop_loss": D(stop),
        "rules": rules(),
        "fee_rate": D(0),
    } | kw
    return size_position(**args)  # type: ignore[arg-type]


def test_quantity_is_capital_times_risk_over_stop_distance() -> None:
    result = size()

    assert isinstance(result, PositionSize)
    assert result.quantity == D(10)
    assert result.risk_amount == D(50)


def test_rounds_down_never_up() -> None:
    result = size(entry="100", stop="97")

    assert isinstance(result, PositionSize)
    assert result.quantity == D("16.666")
    assert result.risk_amount <= D(50)


def test_capped_by_max_position_fraction() -> None:
    result = size(entry="100", stop="99.9", max_position_fraction=D("0.25"))

    assert isinstance(result, PositionSize)
    assert result.quantity == D(25)


def test_rejects_below_min_quantity() -> None:
    result = size(equity="10", entry="60000", stop="59000", rules=rules(min_quantity=D("0.001")))

    assert isinstance(result, Rejection)
    assert result.code is RejectionCode.QUANTITY_BELOW_MIN


def test_rejects_below_min_notional() -> None:
    result = size(equity="100", entry="100", stop="95", rules=rules(min_notional=D(50)))

    assert isinstance(result, Rejection)
    assert result.code is RejectionCode.NOTIONAL_BELOW_MIN


def test_exact_minimum_notional_is_accepted_at_entry_and_at_stop() -> None:
    result = size(equity="100", entry="100", stop="95", rules=rules(min_notional=D("9.5")))

    assert isinstance(result, PositionSize)
    assert result.quantity * D(100) == D(10)
    assert result.quantity * D(95) == D("9.5")


def test_notional_is_also_checked_at_the_stop_price() -> None:
    result = size(equity="100", entry="100", stop="95", rules=rules(min_notional=D("9.6")))

    assert isinstance(result, Rejection)
    assert result.code is RejectionCode.NOTIONAL_BELOW_MIN


def test_round_trip_fees_are_discounted_so_loss_at_stop_stays_within_risk() -> None:
    result = size(fee_rate=D("0.001"))

    assert isinstance(result, PositionSize)
    assert result.quantity == D("9.624")
    stop_loss = result.quantity * (D(100) - D(95)) + D("0.001") * result.quantity * (D(100) + D(95))
    assert stop_loss == result.risk_amount
    assert stop_loss <= D(50)


def test_max_quantity_caps_the_position() -> None:
    result = size(rules=rules(max_quantity=D(3)))

    assert isinstance(result, PositionSize)
    assert result.quantity == D(3)


@pytest.mark.parametrize("field", ["equity", "entry", "stop_loss", "risk_fraction", "fee_rate"])
@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_input_is_rejected_not_raised(field: str, bad: str) -> None:
    args: dict[str, object] = {
        "equity": D(10_000),
        "risk_fraction": HALF_PERCENT,
        "max_position_fraction": D(1),
        "entry": D(100),
        "stop_loss": D(95),
        "rules": rules(),
        "fee_rate": D(0),
    } | {field: D(bad)}

    result = size_position(**args)  # type: ignore[arg-type]

    assert isinstance(result, Rejection)


def test_non_positive_capital_is_rejected() -> None:
    result = size(equity="0")

    assert isinstance(result, Rejection)
    assert result.code is RejectionCode.INVALID_EQUITY


@given(
    equity=st.integers(100, 10_000_000).map(Decimal),
    entry=st.integers(1, 100_000).map(lambda n: Decimal(n) / 100),
    distance=st.integers(1, 99).map(lambda n: Decimal(n) / 100),
    risk=st.sampled_from(["0.001", "0.005", "0.01"]).map(Decimal),
    step=st.sampled_from(["0.00001", "0.001", "1"]).map(Decimal),
    fee=st.sampled_from(["0", "0.001", "0.0075"]).map(Decimal),
)
def test_realized_risk_never_exceeds_budget(
    equity: Decimal, entry: Decimal, distance: Decimal, risk: Decimal, step: Decimal, fee: Decimal
) -> None:
    stop = entry * (1 - distance)
    result = size_position(
        equity=equity,
        risk_fraction=risk,
        max_position_fraction=D("0.25"),
        entry=entry,
        stop_loss=stop,
        rules=rules(step_size=step, min_quantity=D(0), min_notional=D(0)),
        fee_rate=fee,
    )

    if isinstance(result, Rejection):
        assert result.code is RejectionCode.QUANTITY_BELOW_MIN
        return
    assert result.quantity % step == 0
    assert result.quantity * ((entry - stop) + fee * (entry + stop)) <= equity * risk
    assert result.quantity * entry <= equity * D("0.25")
