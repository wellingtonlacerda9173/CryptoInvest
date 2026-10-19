"""Regras endurecidas apos a revisao de QA: fills, calendario, pendentes, taxas, selo e travas."""

import copy
from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError

from engine.application.risk import (
    ApprovalError,
    ApprovedEntry,
    EmergencyExit,
    EventCalendar,
    ExitFill,
    Rejection,
    RejectionCode,
    RiskState,
    close_position,
    evaluate_entry,
    manage_position,
    open_position,
    verify_approval,
)
from engine.application.risk.filters import history_problem
from engine.domain.risk import RiskConfig
from engine.domain.signal import Signal
from engine.domain.trade import ExitReason, Trade
from engine.domain.types import Timeframe
from tests.unit.risk.factories import (
    EMPTY_CALENDAR,
    NOW,
    SYMBOL,
    D,
    candle,
    history,
    market,
    position,
    rules,
    signal,
    trade,
)

NO_FEE = RiskConfig(taker_fee_rate=D(0))


def approve(sig: Signal | None = None, **overrides: object) -> ApprovedEntry | Rejection:
    args: dict[str, object] = {
        "state": RiskState.initial(D(10_000), NOW),
        "config": NO_FEE,
        "rules": rules(),
        "positions": [],
        "pending": [],
        "market": market(),
        "history": history(),
        "calendar": EMPTY_CALENDAR,
        "now": NOW,
        "client_order_id": "e-1",
    } | overrides
    return evaluate_entry(sig or signal(), **args)  # type: ignore[arg-type]


def approved(sig: Signal | None = None, **overrides: object) -> ApprovedEntry:
    result = approve(sig, **overrides)
    assert isinstance(result, ApprovedEntry)
    return result


def code_of(result: ApprovedEntry | Rejection) -> RejectionCode:
    assert isinstance(result, Rejection)
    return result.code


def other(sym: str) -> Signal:
    return signal(symbol=sym)


def sym_args(sym: str) -> dict[str, object]:
    return {
        "rules": rules(symbol=sym),
        "market": market(symbol=sym),
        "history": history(symbol=sym),
    }


class TestOpenPosition:
    def test_fill_within_approval_is_accepted_and_carries_fee(self) -> None:
        pos = open_position(
            approved(config=RiskConfig()),
            opened_at=NOW,
            fill_price=D("99.5"),
            filled_quantity=D(5),
            fee=D("0.45"),
        )

        assert (pos.entry_price, pos.quantity, pos.entry_fee) == (D("99.5"), D(5), D("0.45"))

    def test_fee_above_configured_taker_rate_is_an_emergency_exit(self) -> None:
        result = open_position(
            approved(config=RiskConfig()),
            opened_at=NOW,
            fill_price=D("99.5"),
            filled_quantity=D(5),
            fee=D("0.5"),
        )

        assert isinstance(result, EmergencyExit)

    @pytest.mark.parametrize("bad", ["0", "-1", "NaN"])
    def test_non_positive_or_non_finite_values_are_refused_not_defaulted(self, bad: str) -> None:
        entry = approved()

        with pytest.raises(ValueError, match=r".+"):
            open_position(entry, opened_at=NOW, fill_price=D(bad))
        with pytest.raises(ValueError, match=r".+"):
            open_position(entry, opened_at=NOW, filled_quantity=D(bad))

    def test_exact_limits_are_accepted(self) -> None:
        entry = approved()

        pos = open_position(
            entry, opened_at=NOW, fill_price=entry.order.price, filled_quantity=entry.order.quantity
        )

        assert pos.quantity == entry.order.quantity

    def test_negative_fee_is_refused(self) -> None:
        with pytest.raises(ValueError, match="fee"):
            open_position(approved(), opened_at=NOW, fee=D(-1))

    def test_forged_approval_cannot_open_a_position(self) -> None:
        real = approved()
        forged = object.__new__(ApprovedEntry)
        for name in ("order", "signal", "risk_amount"):
            object.__setattr__(forged, name, getattr(real, name))

        with pytest.raises(ApprovalError):
            open_position(forged, opened_at=NOW)


class TestApprovalSeal:
    def test_issued_approval_verifies(self) -> None:
        entry = approved()

        assert entry.verify() is entry
        assert verify_approval(entry) is entry

    def test_object_new_forgery_fails_verification(self) -> None:
        real = approved()
        forged = object.__new__(ApprovedEntry)

        with pytest.raises(ApprovalError):
            forged.verify()
        for name in ("order", "signal", "risk_amount"):
            object.__setattr__(forged, name, getattr(real, name))
        with pytest.raises(ApprovalError):
            forged.verify()

    def test_tampered_fields_fail_verification(self) -> None:
        real = approved()
        bigger = real.order.model_copy(update={"quantity": real.order.quantity * 10})
        tampered = object.__new__(ApprovedEntry)
        for name in ("signal", "risk_amount", "_seal"):
            object.__setattr__(tampered, name, object.__getattribute__(real, name))
        object.__setattr__(tampered, "order", bigger)

        with pytest.raises(ApprovalError):
            tampered.verify()

    def test_foreign_objects_are_refused_by_the_executor_gate(self) -> None:
        with pytest.raises(ApprovalError):
            verify_approval(object())


class TestEventCalendarCoverage:
    @pytest.mark.parametrize(
        "calendar",
        [
            EventCalendar(),
            EventCalendar(valid_until=NOW + timedelta(days=5), acknowledged_no_events=True),
            EventCalendar(loaded_at=NOW - timedelta(days=5), acknowledged_no_events=True),
            EventCalendar(
                loaded_at=NOW - timedelta(days=9),
                valid_until=NOW - timedelta(seconds=1),
                acknowledged_no_events=True,
            ),
            EventCalendar(loaded_at=NOW - timedelta(days=1), valid_until=NOW + timedelta(days=5)),
            EventCalendar(loaded_at=NOW + timedelta(days=1), valid_until=NOW + timedelta(days=5)),
        ],
        ids=["absent", "no-loaded-at", "no-valid-until", "expired", "empty-unacknowledged", "future-load"],
    )
    def test_missing_expired_or_unconfirmed_calendar_blocks_entries(self, calendar: EventCalendar) -> None:
        assert code_of(approve(calendar=calendar)) is RejectionCode.EVENT_CALENDAR_STALE

    def test_expired_exactly_at_valid_until_is_still_valid(self) -> None:
        calendar = EventCalendar(
            loaded_at=NOW - timedelta(days=1), valid_until=NOW, acknowledged_no_events=True
        )

        assert isinstance(approve(calendar=calendar), ApprovedEntry)

    def test_acknowledged_empty_calendar_with_future_validity_is_accepted(self) -> None:
        assert isinstance(approve(calendar=EMPTY_CALENDAR), ApprovedEntry)

    def test_historical_mode_skips_only_the_coverage_requirement(self) -> None:
        assert isinstance(approve(calendar=EventCalendar(), mode="backtest"), ApprovedEntry)

    def test_historical_mode_still_honours_listed_windows(self) -> None:
        calendar = EventCalendar.model_validate(
            {"windows": [{"start": NOW.isoformat(), "end": (NOW + timedelta(hours=1)).isoformat()}]}
        )

        assert code_of(approve(calendar=calendar, mode="backtest")) is RejectionCode.EVENT_WINDOW

    def test_valid_until_cannot_precede_loaded_at(self) -> None:
        with pytest.raises(ValidationError):
            EventCalendar(loaded_at=NOW, valid_until=NOW - timedelta(days=1))


class TestPendingOrders:
    def test_second_approval_for_same_symbol_without_fill_is_rejected(self) -> None:
        first = approved()

        assert code_of(approve(pending=[first], client_order_id="e-2")) is RejectionCode.ENTRY_PENDING

    def test_pending_counts_toward_max_open_positions(self) -> None:
        pend = [approved(other(s), **sym_args(s)) for s in ("ETH/USDT", "SOL/USDT")]

        assert code_of(approve(pending=pend)) is RejectionCode.MAX_POSITIONS

    def test_positions_and_pending_are_summed_for_the_limit(self) -> None:
        held = position().model_copy(update={"symbol": "ETH/USDT"})
        pend = approved(other("SOL/USDT"), **sym_args("SOL/USDT"))

        assert code_of(approve(positions=[held], pending=[pend])) is RejectionCode.MAX_POSITIONS

    def test_total_exposure_includes_pending_and_is_capped(self) -> None:
        cfg = RiskConfig(
            max_total_exposure_fraction=D("0.15"), max_position_fraction=D("0.1"), taker_fee_rate=D(0)
        )
        pending = approved(other("ETH/USDT"), config=cfg, **sym_args("ETH/USDT"))

        assert code_of(approve(config=cfg, pending=[pending])) is RejectionCode.TOTAL_EXPOSURE

    def test_total_exposure_ceiling_is_hard(self) -> None:
        with pytest.raises(ValidationError):
            RiskConfig(max_total_exposure_fraction=D("0.9"))


class TestFees:
    def test_loss_at_stop_including_round_trip_fees_never_exceeds_approved_risk(self) -> None:
        cfg = RiskConfig()
        entry = approved(config=cfg)
        order = entry.order
        fee = cfg.taker_fee_rate

        loss = order.quantity * (order.price - order.stop_loss) + fee * order.quantity * (
            order.price + order.stop_loss
        )

        assert loss == entry.risk_amount
        assert loss <= cfg.risk_per_trade * D(10_000)

    def test_exit_fill_and_trade_carry_both_fees(self) -> None:
        pos = position(quantity="2").model_copy(update={"entry_fee": D("0.4")})
        fill = ExitFill(quantity=D(1), price=D(110), reason=ExitReason.TAKE_PROFIT, at=NOW, fee=D("0.11"))

        result = fill.to_trade(pos)

        assert (result.entry_fee, result.exit_fee) == (D("0.2"), D("0.11"))
        assert result.pnl == D(10) - D("0.2") - D("0.11")

    def test_manage_position_estimates_exit_fee_with_taker_rate(self) -> None:
        result = manage_position(position(), candle("100", "101", "94", "95"), rules(), RiskConfig())

        [fill] = result.exits
        assert fill.fee == fill.quantity * fill.price * RiskConfig().taker_fee_rate

    def test_close_position_requires_an_explicit_fee(self) -> None:
        pos = position()

        fill = close_position(pos, D(103), NOW, ExitReason.MANUAL, fee=D("0.3"))

        assert fill.to_trade(pos).pnl == D(3) - D("0.3")

    def test_breakeven_stop_covers_entry_and_exit_fees(self) -> None:
        pos = position().model_copy(update={"entry_fee": D("0.1")})
        cfg = RiskConfig(taker_fee_rate=D("0.001"))

        result = manage_position(pos, candle("100", "105", "99", "104"), rules(), cfg)

        assert result.position is not None
        be = result.position.stop
        net = (be - pos.entry_price) * D(1) - pos.entry_fee - be * D(1) * cfg.taker_fee_rate
        assert net >= 0
        assert be < D("100.5")

    def test_breakeven_stop_exit_is_not_a_net_loss(self) -> None:
        pos = position()
        cfg = RiskConfig(taker_fee_rate=D("0.001"))
        armed = manage_position(pos, candle("100", "105", "99", "104"), rules(), cfg).position
        assert armed is not None

        stopped = manage_position(
            armed, candle("104", "104", "90", "95", at=NOW + timedelta(hours=1)), rules(), cfg
        )

        [fill] = stopped.exits
        assert fill.price == armed.stop
        assert fill.to_trade(armed).pnl >= 0

    def test_fee_only_loss_counts_as_a_loss_in_state(self) -> None:
        pos = position()
        fill = ExitFill(quantity=D(1), price=D(100), reason=ExitReason.SIGNAL, at=NOW, fee=D("0.2"))
        fee_only = fill.to_trade(pos)
        cfg = RiskConfig()

        state = RiskState.initial(D(10_000), NOW).record_trade(fee_only, NOW, cfg)

        assert fee_only.pnl == D("-0.2")
        assert state.loss_streaks[SYMBOL] == 1
        assert state.day_pnl == D("-0.2")

    def test_daily_loss_uses_net_of_fees(self) -> None:
        gross_only = Trade(
            symbol=SYMBOL,
            quantity=D(1),
            entry_price=D(1000),
            exit_price=D("900.5"),
            opened_at=NOW,
            closed_at=NOW,
            exit_reason=ExitReason.STOP_LOSS,
        )
        with_fees = gross_only.model_copy(update={"entry_fee": D("0.3"), "exit_fee": D("0.3")})
        cfg = RiskConfig()

        assert (
            RiskState.initial(D(10_000), NOW).record_trade(gross_only, NOW, cfg).entry_block(SYMBOL, NOW)
            is None
        )
        blocked = RiskState.initial(D(10_000), NOW).record_trade(with_fees, NOW, cfg)
        assert blocked.day_pnl == D("-100.1")
        assert blocked.entry_block(SYMBOL, NOW) is not None


class TestMarketAndSignalCoherence:
    def test_only_the_configured_quote_asset_is_accepted(self) -> None:
        sym = "BTC/USDC"

        assert code_of(approve(other(sym), **sym_args(sym))) is RejectionCode.QUOTE_NOT_ALLOWED

    @pytest.mark.parametrize("base", ["BTCUP", "ETHDOWN", "BNBBULL", "XRPBEAR"])
    def test_leveraged_tokens_are_blocked(self, base: str) -> None:
        sym = f"{base}/USDT"

        assert code_of(approve(other(sym), **sym_args(sym))) is RejectionCode.ASSET_NOT_ALLOWED

    def test_real_assets_ending_like_leveraged_tokens_are_not_blocked(self) -> None:
        sym = "JUP/USDT"

        assert isinstance(approve(other(sym), **sym_args(sym)), ApprovedEntry)

    def test_whitelist_restricts_assets_and_blocklist_overrides(self) -> None:
        only_eth = RiskConfig(allowed_assets=frozenset({"ETH"}), taker_fee_rate=D(0))
        blocked = RiskConfig(blocked_assets=frozenset({"BTC"}), taker_fee_rate=D(0))

        assert code_of(approve(config=only_eth)) is RejectionCode.ASSET_NOT_ALLOWED
        assert code_of(approve(config=blocked)) is RejectionCode.ASSET_NOT_ALLOWED
        sym = "ETH/USDT"
        assert isinstance(approve(other(sym), config=only_eth, **sym_args(sym)), ApprovedEntry)

    def test_bid_must_be_above_the_stop(self) -> None:
        assert code_of(approve(market=market(bid="94", ask="94.01"))) is RejectionCode.MARKET_INCOHERENT

    def test_price_must_be_close_to_the_signal_entry(self) -> None:
        far = market(bid="101", ask="101.02")
        near = market(bid="100.4", ask="100.42")

        assert code_of(approve(market=far)) is RejectionCode.MARKET_INCOHERENT
        assert isinstance(approve(market=near), ApprovedEntry)

    def test_entry_deviation_tolerance_is_configurable(self) -> None:
        loose = RiskConfig(max_entry_deviation=D("0.02"), taker_fee_rate=D(0))

        assert isinstance(approve(config=loose, market=market(bid="101", ask="101.02")), ApprovedEntry)

    def test_old_signal_is_rejected_after_n_timeframes(self) -> None:
        at_limit = signal(created_at=NOW - timedelta(hours=2))
        too_old = signal(created_at=NOW - timedelta(hours=2, seconds=1))

        assert isinstance(approve(at_limit), ApprovedEntry)
        assert code_of(approve(too_old)) is RejectionCode.SIGNAL_STALE

    def test_signal_from_the_future_is_inconsistent(self) -> None:
        future = signal(created_at=NOW + timedelta(minutes=1))

        assert code_of(approve(future)) is RejectionCode.INCONSISTENT_INPUT

    def test_stale_or_future_quote_is_rejected(self) -> None:
        assert isinstance(approve(market=market(at=NOW - timedelta(seconds=30))), ApprovedEntry)
        assert code_of(approve(market=market(at=NOW - timedelta(seconds=31)))) is RejectionCode.QUOTE_STALE
        assert code_of(approve(market=market(at=NOW + timedelta(minutes=5)))) is RejectionCode.QUOTE_STALE

    def test_ordering_does_not_hide_the_risk_checks_on_dust_quotes(self) -> None:
        assert code_of(approve(market=market(volume="1"))) is RejectionCode.LOW_LIQUIDITY


class TestPriceFilters:
    def test_levels_are_rounded_to_tick_without_worsening_reward_risk(self) -> None:
        sig = signal(entry="100.004", stop="95.004", target="110.004")

        entry = approved(sig, rules=rules(tick_size=D("0.01")))

        order = entry.order
        assert (order.price, order.stop_loss, order.take_profit) == (D("100.00"), D("95.01"), D("110.01"))
        assert (order.take_profit - order.price) / (order.price - order.stop_loss) >= sig.reward_risk

    def test_max_quantity_caps_the_order(self) -> None:
        assert approved(rules=rules(max_quantity=D(3))).order.quantity == D(3)

    def test_stop_notional_below_minimum_is_rejected(self) -> None:
        assert code_of(approve(rules=rules(min_notional=D("950.5")))) is RejectionCode.NOTIONAL_BELOW_MIN


class TestHistory:
    def test_short_history_is_rejected(self) -> None:
        assert code_of(approve(history=history(19))) is RejectionCode.HISTORY_GAP

    def test_future_candle_is_rejected(self) -> None:
        assert history_problem(history(20, now=NOW + timedelta(hours=1)), SYMBOL, Timeframe.H1, NOW, 20)

    def test_min_history_is_configurable(self) -> None:
        cfg = RiskConfig(min_history_candles=3, taker_fee_rate=D(0))

        assert isinstance(approve(config=cfg, history=history(3)), ApprovedEntry)


class TestManagementGuards:
    def test_open_candle_is_refused(self) -> None:
        bar = candle("100", "101", "99", "100").model_copy(update={"closed": False})

        with pytest.raises(ValueError, match="closed"):
            manage_position(position(), bar, rules(), NO_FEE)

    def test_candle_opening_exactly_at_the_position_open_is_accepted(self) -> None:
        manage_position(position(), candle("100", "101", "99", "100"), rules(), NO_FEE)

    def test_candle_opened_one_second_before_the_position_is_refused(self) -> None:
        pos = position().model_copy(update={"opened_at": NOW + timedelta(seconds=1)})

        with pytest.raises(ValueError, match="before"):
            manage_position(pos, candle("100", "101", "99", "100"), rules(), NO_FEE)

    def test_loss_exit_below_net_entry_is_not_called_profit_protection(self) -> None:
        result = manage_position(position(), candle("100", "105.5", "96", "97"), rules(), NO_FEE)

        [fill] = result.exits
        assert fill.reason is ExitReason.STOP_LOSS

    def test_profit_protection_needs_close_above_net_breakeven(self) -> None:
        cfg = RiskConfig(taker_fee_rate=D("0.01"))

        [below] = manage_position(position(), candle("100", "105.5", "99.5", "101"), rules(), cfg).exits
        [above] = manage_position(position(), candle("100", "105.5", "99.5", "102"), rules(), cfg).exits

        assert below.reason is ExitReason.STOP_LOSS
        assert above.reason is ExitReason.PROFIT_PROTECTION

    def test_partial_always_reports_the_protection_to_replace(self) -> None:
        pos = position().model_copy(
            update={"breakeven_reached": True, "stop": D(100), "highest_price": D(105)}
        )
        cfg = RiskConfig(trailing_r=D(5), taker_fee_rate=D(0))

        result = manage_position(pos, candle("105", "110", "105", "109"), rules(), cfg)

        assert result.exits
        assert result.stop_update == D(100)

    def test_partial_rest_must_meet_notional_at_the_stop(self) -> None:
        lot = rules(min_quantity=D(0), min_notional=D(53))
        pos = position(quantity="1")

        result = manage_position(pos, candle("100", "110", "99", "109"), lot, NO_FEE)

        assert result.exits == ()


class TestStateGuards:
    def test_mappings_are_immutable_in_place(self) -> None:
        state = RiskState.initial(D(10_000), NOW)
        state = state.record_trade(trade("-10"), NOW, NO_FEE)

        with pytest.raises(TypeError):
            state.loss_streaks[SYMBOL] = 0  # type: ignore[index]
        with pytest.raises(TypeError):
            state.loss_streaks.clear()  # type: ignore[attr-defined]
        assert state.loss_streaks[SYMBOL] == 1

    def test_frozen_mappings_survive_copy_and_json(self) -> None:
        state = RiskState.initial(D(10_000), NOW).record_trade(trade("-10"), NOW, NO_FEE)

        assert copy.deepcopy(state) == state
        assert RiskState.model_validate_json(state.model_dump_json()) == state
        with pytest.raises(TypeError):
            RiskState.model_validate_json(state.model_dump_json()).loss_streaks.update({"X/USDT": 1})  # type: ignore[attr-defined]

    @pytest.mark.parametrize(
        "call",
        [
            lambda s, n: s.entry_block(SYMBOL, n),
            lambda s, n: s.roll_day(n),
            lambda s, n: s.observe_equity(D(9_000), n, NO_FEE),
            lambda s, n: s.record_trade(trade("-1"), n, NO_FEE),
            lambda s, n: s.pause("x", n),
            lambda s, n: s.resume(operator="o", reason="r", now=n),
            lambda s, n: s.reactivate(operator="o", reason="r", now=n),
            lambda s, n: RiskState.initial(D(1), n),
        ],
        ids=["entry_block", "roll_day", "observe", "record", "pause", "resume", "reactivate", "initial"],
    )
    def test_naive_datetimes_are_refused_everywhere(self, call: object) -> None:
        naive = datetime(2026, 3, 10, 12, 0)
        state = RiskState.initial(D(10_000), NOW).model_copy(update={"halted": True})

        with pytest.raises(ValueError, match="timezone"):
            call(state, naive)  # type: ignore[operator]

    def test_naive_now_is_refused_by_the_gate(self) -> None:
        with pytest.raises(ValueError, match="timezone"):
            approve(now=datetime(2026, 3, 10, 12, 0))

    def test_equity_drop_pauses_and_is_audited(self) -> None:
        state = RiskState.initial(D(10_000), NOW).observe_equity(D(9_900), NOW, NO_FEE)

        assert state.day_pnl == D(-100)
        assert state.entry_block(SYMBOL, NOW) is not None
        assert state.audit[-1].action.value == "daily_loss_pause"

    def test_equity_rise_never_erases_a_recorded_loss(self) -> None:
        state = RiskState.initial(D(10_000), NOW).record_trade(trade("-60"), NOW, NO_FEE)

        state = state.observe_equity(D(10_000), NOW, NO_FEE)

        assert state.day_pnl == D(-60)

    def test_small_equity_drop_does_not_pause(self) -> None:
        state = RiskState.initial(D(10_000), NOW).observe_equity(D(9_900.01), NOW, NO_FEE)

        assert state.entry_block(SYMBOL, NOW) is None

    def test_record_trade_is_idempotent_by_trade_id(self) -> None:
        first = trade("-10", trade_id="t-1")
        state = RiskState.initial(D(10_000), NOW).record_trade(first, NOW, NO_FEE)

        again = state.record_trade(first, NOW, NO_FEE)

        assert again is state
        assert len(again.ledger) == 1
        assert again.loss_streaks[SYMBOL] == 1

    def test_trades_without_id_are_always_recorded(self) -> None:
        state = RiskState.initial(D(10_000), NOW)

        state = state.record_trade(trade("-1"), NOW, NO_FEE).record_trade(trade("-1"), NOW, NO_FEE)

        assert len(state.ledger) == 2


class TestConfigFloors:
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"pair_pause_hours": 23},
            {"max_open_positions": 3},
            {"consecutive_loss_limit": 3},
            {"consecutive_loss_limit": 0},
            {"max_total_exposure_fraction": D("0.81")},
            {"taker_fee_rate": D("0.02")},
        ],
    )
    def test_loosening_the_spec_is_refused(self, kwargs: dict[str, object]) -> None:
        with pytest.raises(ValidationError):
            RiskConfig(**kwargs)  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "kwargs",
        [{"pair_pause_hours": 48}, {"max_open_positions": 1}, {"consecutive_loss_limit": 1}],
    )
    def test_stricter_values_are_allowed(self, kwargs: dict[str, object]) -> None:
        assert RiskConfig(**kwargs)  # type: ignore[arg-type]

    def test_stricter_loss_limit_pauses_after_one_loss(self) -> None:
        cfg = RiskConfig(consecutive_loss_limit=1, taker_fee_rate=D(0))

        state = RiskState.initial(D(10_000), NOW).record_trade(trade("-1"), NOW, cfg)

        assert state.entry_block(SYMBOL, NOW) is not None
