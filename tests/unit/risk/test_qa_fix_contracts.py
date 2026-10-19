"""Contratos das correcoes da reverificacao 2: R:R liquido, stop clampado, fill, idempotencia, modo."""

from datetime import timedelta
from typing import ClassVar

import pytest
from pydantic import ValidationError

from engine.application.risk import (
    AccountSnapshot,
    ApprovalError,
    ApprovalExpiredError,
    ApprovedEntry,
    EmergencyExit,
    EventCalendar,
    ManagedPosition,
    Rejection,
    RejectionCode,
    RiskState,
    evaluate_entry,
    exit_fill_id,
    ignored_assets_for,
    manage_position,
    open_position,
    reconcile,
)
from engine.application.risk.sizing import ceil_to_step, floor_to_step, net_reward_risk_ok
from engine.application.strategies import ElephantBarBreakout, ElephantBarBreakoutConfig, StrategyContext
from engine.domain.market import Balance
from engine.domain.risk import RiskConfig
from engine.domain.trade import ExitReason
from tests.unit.application.test_strategies import breakout_series
from tests.unit.risk.factories import EMPTY_CALENDAR, NOW, D, candle, history, market, position, rules, signal

FEE = RiskConfig(taker_fee_rate=D("0.001"))


def approve(sig=None, **overrides: object) -> ApprovedEntry | Rejection:  # type: ignore[no-untyped-def]
    args: dict[str, object] = {
        "state": RiskState.initial(D(10_000), NOW),
        "config": FEE,
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


def approved(**overrides: object) -> ApprovedEntry:
    result = approve(**overrides)
    assert isinstance(result, ApprovedEntry)
    return result


class TestNetRewardRisk:
    def test_rejection_carries_dedicated_code(self) -> None:
        result = approve(
            signal(entry="100", stop="99.9", target="100.2"), market=market(bid="99.99", ask="100.01")
        )

        assert isinstance(result, Rejection)
        assert result.code is RejectionCode.NET_REWARD_RISK

    def test_gross_2r_target_is_not_enough_once_fees_are_paid(self) -> None:
        assert not net_reward_risk_ok(D(100), D(95), D(110), D("0.001"), D(2))
        assert net_reward_risk_ok(D(100), D(95), D(111), D("0.001"), D(2))

    def test_zero_fee_matches_the_gross_rule(self) -> None:
        assert net_reward_risk_ok(D(100), D(95), D(110), D(0), D(2))
        assert not net_reward_risk_ok(D(100), D(95), D("109.99"), D(0), D(2))

    def test_boundary_is_inclusive(self) -> None:
        fee, entry, stop = D("0.001"), D(100), D(95)
        loss = (entry - stop) + fee * (entry + stop)
        # reward = (t - 100) - fee * (100 + t) = 2 * loss  =>  t = (2 * loss + 100 * (1 + fee)) / (1 - fee)
        target = (2 * loss + entry * (1 + fee)) / (1 - fee)

        assert net_reward_risk_ok(entry, stop, target, fee, D(2))
        assert not net_reward_risk_ok(entry, stop, target - D("0.0001"), fee, D(2))


class TestStopNeverAboveMarket:
    def test_stop_stays_at_least_one_tick_below_the_close(self) -> None:
        pos = position(entry="100", stop="99.9", target="100.2")

        result = manage_position(pos, candle("100", "100.1", "100.0", "100.09"), rules(), FEE)

        assert result.position is not None
        assert result.stop_update == D("100.08")
        assert result.position.stop == D("100.08")

    def test_breakeven_above_the_market_is_clamped_to_the_allowed_level(self) -> None:
        pos = position(entry="100", stop="99.9", target="100.2")

        result = manage_position(pos, candle("100", "100.1", "100.0", "100.1"), rules(), FEE)

        assert result.position is not None
        assert D("99.9") < result.position.stop <= D("100.1")
        assert result.stop_update == result.position.stop

    def test_reachable_breakeven_still_moves_the_stop(self) -> None:
        pos = position(entry="100", stop="95", target="130")

        result = manage_position(pos, candle("100", "106", "100", "105"), rules(), FEE)

        assert result.stop_update is not None
        assert D(100) < result.stop_update <= D(105)


class TestFillContract:
    def test_fee_at_the_cap_is_accepted_and_above_is_refused(self) -> None:
        entry = approved()
        notional = entry.order.price * entry.order.quantity
        cap = FEE.max_entry_fee_multiple * FEE.taker_fee_rate * notional

        assert isinstance(open_position(entry, opened_at=NOW, fee=cap), ManagedPosition)
        result = open_position(entry, opened_at=NOW, fee=cap + D("0.01"))
        assert isinstance(result, EmergencyExit)
        assert "taxa anormal" in result.reason
        assert result.entry_fee == cap + D("0.01")

    def test_fee_multiple_is_configurable(self) -> None:
        entry = approved(config=RiskConfig(taker_fee_rate=D("0.001"), max_entry_fee_multiple=D(5)))
        notional = entry.order.price * entry.order.quantity

        assert isinstance(open_position(entry, opened_at=NOW, fee=D("0.005") * notional), ManagedPosition)

    def test_approval_expires_after_the_ttl(self) -> None:
        entry = approved()
        ttl = entry.guards.expires_at

        assert isinstance(open_position(entry, opened_at=ttl), ManagedPosition)
        result = open_position(entry, opened_at=ttl + timedelta(seconds=1))
        assert isinstance(result, EmergencyExit)
        assert result.action == "SELL_MARKET_IMMEDIATELY"
        assert "expirada" in result.reason

    def test_expired_error_is_an_approval_error_and_a_value_error(self) -> None:
        assert issubclass(ApprovalExpiredError, ApprovalError)
        assert issubclass(ApprovalExpiredError, ValueError)

    def test_fill_before_approval_or_naive_time_is_refused(self) -> None:
        entry = approved()

        with pytest.raises(ValueError, match="precede"):
            open_position(entry, opened_at=NOW - timedelta(seconds=1))
        with pytest.raises(ValueError, match="timezone-aware"):
            open_position(entry, opened_at=NOW.replace(tzinfo=None))

    def test_tampered_guards_break_the_seal(self) -> None:
        entry = approved()
        object.__setattr__(
            entry, "guards", entry.guards.model_copy(update={"expires_at": NOW + timedelta(days=90)})
        )

        with pytest.raises(ApprovalError):
            open_position(entry, opened_at=NOW + timedelta(days=30))

    @pytest.mark.parametrize("fill", ["95", "94", "50"])
    def test_fill_at_or_below_the_stop_is_an_explicit_emergency_exit(self, fill: str) -> None:
        entry = approved()

        result = open_position(
            entry, opened_at=NOW, fill_price=D(fill), filled_quantity=D(2), fee=D("0.00001")
        )

        assert isinstance(result, EmergencyExit)
        assert result.action == "SELL_MARKET_IMMEDIATELY"
        assert (result.symbol, result.quantity, result.fill_price) == (entry.order.symbol, D(2), D(fill))
        assert result.stop_loss == entry.order.stop_loss
        assert result.entry_fee == D("0.00001")

    def test_dust_below_the_stop_is_reported_as_dust_not_as_a_sellable_exit(self) -> None:
        result = open_position(approved(), opened_at=NOW, fill_price=D("0.01"), filled_quantity=D(2))

        assert isinstance(result, Rejection)

    def test_fill_just_above_the_stop_is_a_managed_position(self) -> None:
        result = open_position(approved(), opened_at=NOW, fill_price=D("95.01"))

        assert isinstance(result, ManagedPosition)

    def test_dust_fill_is_a_rejection_not_a_position(self) -> None:
        by_quantity = open_position(approved(), opened_at=NOW, filled_quantity=D("0.0005"))
        by_notional = open_position(approved(), opened_at=NOW, filled_quantity=D("0.05"))

        assert isinstance(by_quantity, Rejection)
        assert by_quantity.code is RejectionCode.QUANTITY_BELOW_MIN
        assert isinstance(by_notional, Rejection)
        assert by_notional.code is RejectionCode.NOTIONAL_BELOW_MIN

    def test_fill_exactly_at_the_minimum_notional_is_accepted(self) -> None:
        entry = approved(rules=rules(min_notional=D(10), step_size=D("0.001"), min_quantity=D("0.001")))

        result = open_position(entry, opened_at=NOW, fill_price=D(100), filled_quantity=D("0.1"))

        assert isinstance(result, ManagedPosition)


class TestIdempotentExits:
    def test_retry_of_the_same_candle_yields_identical_fill_ids(self) -> None:
        pos = position(entry="100", stop="95", target="110")
        bar = candle("100", "112", "100", "103")

        first = manage_position(pos, bar, rules(), FEE)
        retry = manage_position(pos, bar, rules(), FEE)

        assert [e.fill_id for e in first.exits] == [e.fill_id for e in retry.exits]
        assert all(e.fill_id for e in first.exits)

    def test_partial_and_final_exit_in_one_candle_get_distinct_ids(self) -> None:
        pos = position(entry="100", stop="95", target="110", quantity="10")

        result = manage_position(pos, candle("100", "120", "100", "103"), rules(), FEE)

        ids = [e.fill_id for e in result.exits]
        assert len(ids) == 2
        assert len(set(ids)) == 2

    def test_ids_differ_across_positions_candles_and_reasons(self) -> None:
        pos = position()
        later = pos.model_copy(update={"opened_at": NOW + timedelta(hours=5)})
        at = NOW + timedelta(hours=1)

        ids = {
            exit_fill_id(pos, at, ExitReason.STOP_LOSS),
            exit_fill_id(later, at, ExitReason.STOP_LOSS),
            exit_fill_id(pos, at + timedelta(hours=1), ExitReason.STOP_LOSS),
            exit_fill_id(pos, at, ExitReason.TAKE_PROFIT),
        }

        assert len(ids) == 4

    def test_record_trade_retry_does_not_double_count_the_loss(self) -> None:
        pos = position(entry="100", stop="95", target="110")
        [exit_fill] = manage_position(pos, candle("100", "100", "90", "92"), rules(), FEE).exits
        trade = exit_fill.to_trade(pos)

        once = RiskState.initial(D(10_000), NOW).record_trade(trade, NOW + timedelta(hours=1), FEE)
        twice = once.record_trade(trade, NOW + timedelta(hours=1), FEE)

        assert twice == once
        assert len(twice.ledger) == 1


class TestObserveEquityVersusRecordTrade:
    def _loss(self, closed_at):  # type: ignore[no-untyped-def]
        from tests.unit.risk.factories import trade

        return trade("-10", at=closed_at, trade_id="t-1")

    def test_trade_already_in_the_observed_equity_is_not_counted_twice(self) -> None:
        state = RiskState.initial(D(10_000), NOW)
        later = NOW + timedelta(minutes=5)

        state = state.observe_equity(D(9_990), later, FEE)
        state = state.record_trade(self._loss(NOW + timedelta(minutes=1)), later, FEE)

        assert state.equity == D(9_990)
        assert state.day_pnl == D(-10)
        assert len(state.ledger) == 1

    def test_already_observed_loss_still_feeds_the_loss_streak(self) -> None:
        state = RiskState.initial(D(10_000), NOW).observe_equity(D(9_990), NOW + timedelta(minutes=5), FEE)

        state = state.record_trade(self._loss(NOW + timedelta(minutes=1)), NOW + timedelta(minutes=5), FEE)

        assert state.loss_streaks["BTC/USDT"] == 1

    def test_trade_closed_after_the_observation_counts_normally(self) -> None:
        state = RiskState.initial(D(10_000), NOW).observe_equity(D(9_990), NOW, FEE)

        state = state.record_trade(self._loss(NOW + timedelta(minutes=1)), NOW + timedelta(minutes=2), FEE)

        assert state.equity == D(9_980)
        assert state.day_pnl == D(-20)

    def test_trade_recorded_before_observation_is_not_counted_again_by_it(self) -> None:
        state = RiskState.initial(D(10_000), NOW).record_trade(self._loss(NOW), NOW, FEE)

        state = state.observe_equity(D(9_990), NOW + timedelta(minutes=1), FEE)

        assert state.equity == D(9_990)
        assert state.day_pnl == D(-10)

    @pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity", "-1"])
    def test_invalid_equity_is_a_clear_error(self, bad: str) -> None:
        with pytest.raises(ValueError, match="equity"):
            RiskState.initial(D(10_000), NOW).observe_equity(D(bad), NOW, FEE)


class TestStepRounding:
    @pytest.mark.parametrize("step", ["0", "-0.01", "NaN", "Infinity"])
    def test_invalid_step_is_a_clear_error(self, step: str) -> None:
        with pytest.raises(ValueError, match="arredondamento"):
            floor_to_step(D(1), D(step))
        with pytest.raises(ValueError, match="arredondamento"):
            ceil_to_step(D(1), D(step))

    @pytest.mark.parametrize("value", ["NaN", "Infinity", "-1"])
    def test_invalid_value_is_a_clear_error(self, value: str) -> None:
        with pytest.raises(ValueError, match="arredondamento"):
            floor_to_step(D(value), D("0.01"))

    def test_extreme_tick_exceeding_decimal_precision_is_a_value_error(self) -> None:
        with pytest.raises(ValueError, match="precisao"):
            floor_to_step(D("1e40"), D("1e-10"))

    def test_exact_multiples_and_zero_are_stable(self) -> None:
        assert floor_to_step(D("0.3"), D("0.1")) == D("0.3")
        assert ceil_to_step(D("0.3"), D("0.1")) == D("0.3")
        assert ceil_to_step(D("0.31"), D("0.1")) == D("0.4")
        assert floor_to_step(D(0), D("0.1")) == D(0)

    def test_gate_turns_an_extreme_tick_into_a_rejection(self) -> None:
        result = approve(rules=rules(tick_size=D("1e-30")))

        assert isinstance(result, Rejection)
        assert result.code is RejectionCode.PRICE_FILTER

    def test_gate_rejects_a_tick_larger_than_the_price(self) -> None:
        result = approve(rules=rules(tick_size=D(1_000)))

        assert isinstance(result, Rejection)
        assert result.code is RejectionCode.PRICE_FILTER


class TestRunMode:
    def test_default_is_live_and_requires_calendar_coverage(self) -> None:
        result = approve(calendar=EventCalendar())

        assert isinstance(result, Rejection)
        assert result.code is RejectionCode.EVENT_CALENDAR_STALE

    def test_backtest_must_be_requested_explicitly(self) -> None:
        assert isinstance(approve(calendar=EventCalendar(), mode="backtest"), ApprovedEntry)

    def test_legacy_boolean_flag_no_longer_exists(self) -> None:
        with pytest.raises(TypeError):
            approve(historical=True)

    def test_unknown_mode_is_refused(self) -> None:
        with pytest.raises(ValueError, match="mode"):
            approve(mode="paper")


class TestBnbReconciliation:
    BNB: ClassVar[dict[str, Balance]] = {"BNB": Balance(asset="BNB", free=D(3), locked=D(0))}

    def test_bnb_is_ignored_while_the_gate_blocks_it(self) -> None:
        assert "BNB" in ignored_assets_for(RiskConfig())
        assert reconcile([], AccountSnapshot(balances=self.BNB)) == ()

    def test_bnb_is_checked_when_the_robot_operates_it(self) -> None:
        config = RiskConfig(allowed_assets=frozenset({"BNB"}), blocked_assets=frozenset())

        assert "BNB" not in ignored_assets_for(config)
        [found] = reconcile([], AccountSnapshot(balances=self.BNB), config=config)
        assert found.subject == "BNB"

    def test_bnb_in_the_whitelist_is_not_ignored(self) -> None:
        config = RiskConfig(allowed_assets=frozenset({"BNB", "BTC"}), blocked_assets=frozenset())

        assert "BNB" not in ignored_assets_for(config)

    def test_bnb_outside_a_whitelist_is_ignored(self) -> None:
        assert "BNB" in ignored_assets_for(RiskConfig(allowed_assets=frozenset({"BTC"})))

    def test_stablecoins_and_quote_asset_are_always_ignored(self) -> None:
        config = RiskConfig(quote_asset="FDUSD", blocked_assets=frozenset())

        assert {"USDT", "USDC", "BUSD", "FDUSD"} <= ignored_assets_for(config)

    def test_explicit_ignored_assets_win_over_the_config(self) -> None:
        found = reconcile([], AccountSnapshot(balances=self.BNB), ignored_assets=frozenset())

        assert [d.subject for d in found] == ["BNB"]

    def test_an_asset_with_a_local_position_is_never_ignored(self) -> None:
        pos = ManagedPosition(
            symbol="BNB/USDT",
            entry_price=D(600),
            initial_quantity=D(1),
            quantity=D(1),
            initial_stop=D(570),
            stop=D(570),
            take_profit=D(700),
            highest_price=D(600),
            opened_at=NOW,
        )

        found = reconcile([pos], AccountSnapshot(balances={}), ignored_assets=frozenset({"BNB"}))

        assert any(d.subject == "BNB" and d.kind.value == "missing_on_exchange" for d in found)


class TestElephantVetoesAndConfluences:
    def test_strong_volume_must_exceed_the_detector_threshold(self) -> None:
        with pytest.raises(ValidationError):
            ElephantBarBreakoutConfig(strong_volume_multiple=1.5)

    def test_rsi_bounds_must_be_ordered(self) -> None:
        with pytest.raises(ValidationError):
            ElephantBarBreakoutConfig(rsi_min=80, rsi_max=75)

    def test_baseline_series_still_signals(self) -> None:
        assert ElephantBarBreakout().evaluate(breakout_series(), StrategyContext()) is not None

    def test_overbought_rsi_is_a_veto_not_a_missing_point(self) -> None:
        config = ElephantBarBreakoutConfig(rsi_min=1, rsi_max=2)

        assert ElephantBarBreakout(config).evaluate(breakout_series(), StrategyContext()) is None

    def test_negative_filters_never_appear_as_confluences(self) -> None:
        result = ElephantBarBreakout().evaluate(breakout_series(), StrategyContext())

        assert result is not None
        assert not {"rsi_com_espaco", "sem_exaustao"} & set(result.confluences)

    def test_confidence_does_not_count_volume_twice(self) -> None:
        result = ElephantBarBreakout().evaluate(breakout_series(), StrategyContext())
        loose = ElephantBarBreakout(ElephantBarBreakoutConfig(strong_volume_multiple=50.0)).evaluate(
            breakout_series(), StrategyContext()
        )

        assert result is not None
        assert loose is None or loose.confidence == result.confidence
