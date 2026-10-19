import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from engine.application.risk import (
    ApprovedEntry,
    EventCalendar,
    EventWindow,
    Rejection,
    RejectionCode,
    RiskState,
    evaluate_entry,
    open_position,
)
from engine.domain.decision import DecisionAction
from engine.domain.order import EntryOrder, revalidate_order
from engine.domain.risk import RiskConfig
from tests.unit.risk.factories import (
    EMPTY_CALENDAR,
    NOW,
    D,
    history,
    market,
    position,
    rules,
    signal,
)

CFG = RiskConfig(taker_fee_rate=D(0))


def evaluate(**overrides: object) -> ApprovedEntry | Rejection:
    now = overrides.get("now", NOW)
    assert isinstance(now, datetime)
    sig = overrides.pop("signal", None) or signal(created_at=now)
    args: dict[str, object] = {
        "state": RiskState.initial(D(10_000), now),
        "config": CFG,
        "rules": rules(),
        "positions": [],
        "pending": [],
        "market": market(at=now),
        "history": history(now=now),
        "calendar": EMPTY_CALENDAR,
        "now": now,
        "client_order_id": "e-1",
    } | overrides
    return evaluate_entry(sig, **args)  # type: ignore[arg-type]


def rejected(result: ApprovedEntry | Rejection) -> RejectionCode:
    assert isinstance(result, Rejection)
    return result.code


def test_approved_entry_carries_oco_levels_and_sized_quantity() -> None:
    result = evaluate()

    assert isinstance(result, ApprovedEntry)
    order = result.order
    assert isinstance(order, EntryOrder)
    assert (order.quantity, order.stop_loss, order.take_profit) == (D(10), D(95), D(111))
    assert result.risk_amount == D(50)
    assert revalidate_order(order) == order
    assert result.to_decision().action is DecisionAction.ENTER


def test_approved_entry_cannot_be_forged_or_mutated() -> None:
    approved = evaluate()
    assert isinstance(approved, ApprovedEntry)

    with pytest.raises(TypeError):
        ApprovedEntry(approved.order, approved.signal, D(1))
    with pytest.raises(TypeError):
        ApprovedEntry(approved.order, approved.signal, D(1), _issuer=object())
    with pytest.raises(AttributeError):
        approved.order = approved.order


def test_rejection_becomes_skip_decision_with_code() -> None:
    result = evaluate(market=market(volume="1"))
    assert isinstance(result, Rejection)

    decision = result.to_decision(signal(), NOW)

    assert decision.action is DecisionAction.SKIP
    assert decision.reason.startswith("low_liquidity")
    assert decision.source == "risk"


def test_blocked_by_state() -> None:
    halted = RiskState.initial(D(10_000), NOW).model_copy(update={"halted": True, "halt_reason": "x"})

    assert rejected(evaluate(state=halted)) is RejectionCode.ROBOT_HALTED


def test_max_open_positions_default_is_two() -> None:
    held = [position().model_copy(update={"symbol": s}) for s in ("ETH/USDT", "SOL/USDT")]

    assert rejected(evaluate(positions=held)) is RejectionCode.MAX_POSITIONS


def test_same_symbol_open_is_rejected() -> None:
    assert rejected(evaluate(positions=[position()])) is RejectionCode.POSITION_ALREADY_OPEN


def test_adding_to_a_loser_is_rejected_with_specific_code() -> None:
    losing_market = market(bid="96", ask="96.01")

    assert rejected(evaluate(positions=[position()], market=losing_market)) is RejectionCode.ADDING_TO_LOSER


def test_coin_exposure_limit_counts_other_quote_pairs() -> None:
    held = position(entry="100", quantity="45").model_copy(update={"symbol": "BTC/USDC"})
    cfg = RiskConfig(max_coin_exposure_fraction=D("0.5"), max_position_fraction=D("0.5"), taker_fee_rate=D(0))

    assert rejected(evaluate(positions=[held], config=cfg)) is RejectionCode.COIN_EXPOSURE


def test_coin_exposure_exact_limit_is_accepted() -> None:
    held = position(entry="100", quantity="40").model_copy(update={"symbol": "BTC/USDC"})
    cfg = RiskConfig(max_coin_exposure_fraction=D("0.5"), max_position_fraction=D("0.5"), taker_fee_rate=D(0))

    assert isinstance(evaluate(positions=[held], config=cfg), ApprovedEntry)


def test_spread_limit_boundary() -> None:
    at_limit = market(bid="99.9", ask="100.1")
    over = market(bid="99.89", ask="100.11")

    assert at_limit.spread == D("0.002")
    assert isinstance(evaluate(market=at_limit), ApprovedEntry)
    assert rejected(evaluate(market=over)) is RejectionCode.SPREAD_TOO_WIDE


def test_liquidity_boundary() -> None:
    assert isinstance(evaluate(market=market(volume="1000000")), ApprovedEntry)
    assert rejected(evaluate(market=market(volume="999999.99"))) is RejectionCode.LOW_LIQUIDITY


def test_history_with_gap_does_not_trade() -> None:
    candles = history(6)
    del candles[2]

    assert rejected(evaluate(history=candles)) is RejectionCode.HISTORY_GAP


@pytest.mark.parametrize("candles", [[], history(3)[::-1]])
def test_empty_or_unordered_history_does_not_trade(candles: list) -> None:  # type: ignore[type-arg]
    assert rejected(evaluate(history=candles)) is RejectionCode.HISTORY_GAP


def test_stale_history_does_not_trade() -> None:
    assert rejected(evaluate(now=NOW + timedelta(hours=3), history=history())) is RejectionCode.HISTORY_GAP


def test_event_window_with_margins() -> None:
    window = EventWindow(start=NOW + timedelta(hours=1), end=NOW + timedelta(hours=2), label="FOMC")
    calendar = EventCalendar(
        windows=(window,), loaded_at=NOW - timedelta(days=1), valid_until=NOW + timedelta(days=30)
    )

    assert isinstance(evaluate(calendar=calendar), ApprovedEntry)
    inside_margin = NOW + timedelta(minutes=30)
    assert rejected(evaluate(calendar=calendar, now=inside_margin)) is RejectionCode.EVENT_WINDOW
    after = window.end + timedelta(minutes=30)
    assert rejected(evaluate(calendar=calendar, now=after)) is RejectionCode.EVENT_WINDOW
    later = after + timedelta(seconds=1)
    assert isinstance(evaluate(calendar=calendar, now=later), ApprovedEntry)


def test_calendar_loads_from_json_and_shipped_file_fails_closed(tmp_path: Path) -> None:
    file = tmp_path / "events.json"
    file.write_text(
        json.dumps(
            {
                "loaded_at": "2026-03-01T00:00:00Z",
                "valid_until": "2026-04-01T00:00:00Z",
                "windows": [
                    {"start": "2026-03-18T18:00:00Z", "end": "2026-03-18T19:30:00Z", "label": "FOMC"}
                ],
            }
        )
    )

    loaded = EventCalendar.load(file)
    shipped = EventCalendar.load(Path("config/volatility_events.json"))

    assert loaded.windows[0].label == "FOMC"
    assert loaded.coverage_problem(NOW) is None
    assert shipped.windows == ()
    assert rejected(evaluate(calendar=shipped)) is RejectionCode.EVENT_CALENDAR_STALE


def test_sizing_rejection_propagates() -> None:
    assert rejected(evaluate(rules=rules(min_notional=D(100_000)))) is RejectionCode.NOTIONAL_BELOW_MIN


def test_reward_risk_below_configured_minimum_is_rejected() -> None:
    cfg = RiskConfig(min_reward_risk=D(3))

    assert rejected(evaluate(config=cfg)) is RejectionCode.REWARD_RISK


def test_symbol_mismatch_is_rejected() -> None:
    assert rejected(evaluate(market=market(symbol="ETH/USDT"))) is RejectionCode.INCONSISTENT_INPUT


def test_open_position_from_approval_starts_protected() -> None:
    approved = evaluate()
    assert isinstance(approved, ApprovedEntry)

    pos = open_position(approved, opened_at=NOW)

    assert (pos.stop, pos.initial_stop, pos.take_profit, pos.quantity) == (D(95), D(95), D(111), D(10))
