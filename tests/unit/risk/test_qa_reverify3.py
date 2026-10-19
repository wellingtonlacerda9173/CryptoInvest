"""Reverificacao 3: integracao estrategia x gate, interacoes de estado e invariantes da gestao de posicao."""

import math
import random
from datetime import timedelta
from decimal import Decimal

import pytest

from engine.application.indicators import CandleArrays, ema
from engine.application.risk import (
    AccountSnapshot,
    ApprovedEntry,
    EmergencyExit,
    ManagedPosition,
    Rejection,
    RiskState,
    evaluate_entry,
    manage_position,
    open_position,
    reconcile,
)
from engine.application.strategies import ElephantBarBreakout, EmaCrossTrend, StrategyContext
from engine.domain.market import Balance
from engine.domain.risk import RiskConfig
from engine.domain.trade import ExitReason
from tests.unit.application.helpers import build_candles, candle_row, reversal_rows
from tests.unit.application.test_strategies import breakout_series
from tests.unit.risk.factories import EMPTY_CALENDAR, NOW, D, candle, history, market, position, rules, trade

CONFIG = RiskConfig()


def _gate(sig, **overrides):  # type: ignore[no-untyped-def]
    args = {
        "state": RiskState.initial(D(10_000), NOW),
        "config": CONFIG,
        "rules": rules(),
        "positions": [],
        "pending": [],
        "market": market(),
        "history": history(),
        "calendar": EMPTY_CALENDAR,
        "now": NOW,
        "client_order_id": "e-1",
    } | overrides
    return evaluate_entry(sig, **args)


def _retime(sig):  # type: ignore[no-untyped-def]
    return sig.model_copy(update={"created_at": NOW, "entry": D(100)})


class TestStrategyToGateIntegration:
    def test_default_elephant_signal_is_not_always_rejected_by_net_rr(self) -> None:
        result = ElephantBarBreakout().evaluate(breakout_series(), StrategyContext())
        assert result is not None
        sig = result.signal
        from engine.application.risk.sizing import net_reward_risk_ok

        assert net_reward_risk_ok(
            sig.entry, sig.stop_loss, sig.take_profit, CONFIG.taker_fee_rate, CONFIG.min_reward_risk
        )

    def test_default_ema_cross_signal_is_not_always_rejected_by_net_rr(self) -> None:
        candles = build_candles(reversal_rows())
        result = None
        for end in range(40, len(candles) + 1):
            result = EmaCrossTrend().evaluate(candles[:end], StrategyContext(htf_trend="up"))
            if result is not None:
                break
        assert result is not None
        sig = result.signal
        from engine.application.risk.sizing import net_reward_risk_ok

        assert net_reward_risk_ok(
            sig.entry, sig.stop_loss, sig.take_profit, CONFIG.taker_fee_rate, CONFIG.min_reward_risk
        )


class TestObserveEquityOrdering:
    def test_stale_observation_does_not_move_the_observation_clock_backwards(self) -> None:
        state = RiskState.initial(D(10_000), NOW)
        state = state.observe_equity(D(10_000), NOW + timedelta(hours=2), CONFIG)

        state = state.observe_equity(D(10_000), NOW + timedelta(hours=1), CONFIG)

        assert state.equity_observed_at == NOW + timedelta(hours=2)


class TestReconcileFeeAsset:
    def test_bnb_fee_balance_not_flagged_when_blocked_by_default(self) -> None:
        snapshot = AccountSnapshot(
            balances={"BNB": Balance(asset="BNB", free=D("2"), locked=D(0))}, prices={"BNB": D(600)}
        )

        assert reconcile([], snapshot) == ()

    def test_bnb_fee_balance_when_blocked_assets_overridden(self) -> None:
        config = RiskConfig(blocked_assets=frozenset({"LUNA"}))
        snapshot = AccountSnapshot(
            balances={"BNB": Balance(asset="BNB", free=D("2"), locked=D(0))}, prices={"BNB": D(600)}
        )

        assert reconcile([], snapshot, config=config) == ()


class TestFillOnLateOrAbnormal:
    def _approved(self) -> ApprovedEntry:
        from engine.application.risk.sizing import SymbolRules  # noqa: F401

        sig = _retime(__import__("tests.unit.risk.factories", fromlist=["signal"]).signal(target="111"))
        out = _gate(sig)
        assert isinstance(out, ApprovedEntry)
        return out

    def test_late_fill_is_an_emergency_exit_because_the_coin_is_already_held(self) -> None:
        approved = self._approved()

        result = open_position(approved, opened_at=approved.guards.expires_at + timedelta(seconds=1))

        assert isinstance(result, EmergencyExit)
        assert result.action == "SELL_MARKET_IMMEDIATELY"
        assert result.quantity == approved.order.quantity
        assert "expirada" in result.reason

    def test_abnormal_fee_is_an_emergency_exit_with_the_paid_fee(self) -> None:
        approved = self._approved()

        result = open_position(approved, opened_at=NOW, fee=D(1_000))

        assert isinstance(result, EmergencyExit)
        assert result.entry_fee == D(1_000)
        assert "taxa anormal" in result.reason

    def test_corrupted_fill_data_is_still_a_value_error(self) -> None:
        approved = self._approved()

        with pytest.raises(ValueError, match=r".+"):
            open_position(approved, opened_at=NOW, fill_price=D("NaN"))
        with pytest.raises(ValueError, match=r".+"):
            open_position(approved, opened_at=NOW, filled_quantity=D(0))

    def test_fill_exactly_at_expiry_is_accepted(self) -> None:
        approved = self._approved()

        result = open_position(approved, opened_at=approved.guards.expires_at)

        assert isinstance(result, ManagedPosition)

    def test_fill_below_stop_with_dust_quantity_is_dust_not_emergency(self) -> None:
        approved = self._approved()

        result = open_position(
            approved,
            opened_at=NOW,
            fill_price=approved.order.stop_loss - D(1),
            filled_quantity=D("0.001"),
        )

        assert isinstance(result, Rejection)

    def test_fill_below_stop_returns_emergency_exit_even_with_boundary_price(self) -> None:
        approved = self._approved()

        result = open_position(approved, opened_at=NOW, fill_price=approved.order.stop_loss)

        assert isinstance(result, EmergencyExit)


class TestManagePositionInvariants:
    @pytest.mark.parametrize("seed", range(40))
    def test_random_walk_keeps_stop_monotonic_and_below_close(self, seed: int) -> None:
        rnd = random.Random(seed)  # noqa: S311
        pos: ManagedPosition | None = position(entry="100", stop="97", target="106", quantity="1")
        price = D(100)
        at = NOW
        total_exit_qty = D(0)
        ids: set[str] = set()
        for _ in range(60):
            if pos is None:
                break
            at = at + timedelta(hours=1)
            o = price
            c = max(D("1"), (o * D(str(1 + rnd.uniform(-0.03, 0.035)))).quantize(D("0.01")))
            hi = max(o, c) + D(str(round(rnd.uniform(0, 1.2), 2)))
            lo = min(o, c) - D(str(round(rnd.uniform(0, 1.2), 2)))
            cd = candle(str(o), str(hi), str(max(lo, D("0.5"))), str(c), at=at)
            result = manage_position(pos, cd, rules(), CONFIG)
            for e in result.exits:
                assert e.fill_id is not None
                assert e.fill_id not in ids
                ids.add(e.fill_id)
                total_exit_qty += e.quantity
            if result.position is not None:
                new = result.position
                assert new.stop >= pos.stop
                if new.stop > pos.stop:
                    assert new.stop < cd.close, (new.stop, cd.close)
                    assert result.stop_update == new.stop
                assert new.stop >= new.initial_stop
            price = c
            pos = result.position
        assert total_exit_qty <= D(1)


class TestElephantTrendRequirement:
    def test_no_signal_when_price_below_trend_ema_and_no_htf_support(self) -> None:
        rows = []
        price = 140.0
        for i in range(70):
            nxt = price - 0.15 + 1.0 * math.sin(i * 1.1)
            rows.append(candle_row(price, nxt, 100 + (i % 5) * 2, 0.15))
            price = nxt
        candles = build_candles([*rows, candle_row(price, price + 3, 300.0, 0.1)])
        data = CandleArrays.from_candles(candles)
        assert float(data.close[-1]) < float(ema(data.close, 50)[-1])

        result = ElephantBarBreakout().evaluate(candles, StrategyContext())

        assert result is None


def test_exit_reason_enum_is_used() -> None:
    assert ExitReason.PROFIT_PROTECTION.value == "profit_protection"
    assert trade("1").pnl > Decimal(0)


def _run_up_series(run: float, body: float):  # type: ignore[no-untyped-def]
    from tests.unit.application.helpers import wave_rows

    rows, price = wave_rows(60)
    for i in range(10):
        nxt = price + run
        rows.append(candle_row(price, nxt, 100 + (i % 5) * 2, 0.15))
        price = nxt
    return build_candles([*rows, candle_row(price, price + body, 300.0, 0.1)])


class TestElephantVetoes:
    def test_overbought_rsi_vetoes_an_otherwise_valid_breakout(self) -> None:
        from engine.application.strategies import ElephantBarBreakoutConfig

        candles = _run_up_series(0.3, 2.5)
        ctx = StrategyContext(htf_trend="up")

        assert (
            ElephantBarBreakout(ElephantBarBreakoutConfig(rsi_max=100.0)).evaluate(candles, ctx) is not None
        )
        assert ElephantBarBreakout().evaluate(candles, ctx) is None

    def test_exhaustion_vetoes_even_when_rsi_cap_is_disabled(self) -> None:
        from engine.application.detectors import ElephantContext, detect_elephant_bar
        from engine.application.strategies import ElephantBarBreakoutConfig

        candles = _run_up_series(0.5, 2.5)
        bar = detect_elephant_bar(candles, ElephantBarBreakoutConfig().elephant)
        assert bar is not None
        assert ElephantContext.EXHAUSTION in bar.contexts

        result = ElephantBarBreakout(ElephantBarBreakoutConfig(rsi_max=100.0)).evaluate(
            candles, StrategyContext(htf_trend="up")
        )

        assert result is None


class TestApprovalTtlAndObservationBoundary:
    def test_ttl_is_exactly_max_signal_age_timeframes_after_approval(self) -> None:
        from tests.unit.risk.factories import signal

        out = _gate(signal())
        assert isinstance(out, ApprovedEntry)

        assert out.guards.approved_at == NOW
        assert out.guards.expires_at == NOW + timedelta(hours=1) * CONFIG.max_signal_age_timeframes

    def test_trade_closed_exactly_at_the_observation_instant_is_not_counted_twice(self) -> None:
        state = RiskState.initial(D(10_000), NOW).observe_equity(D(9_900), NOW, CONFIG)

        state = state.record_trade(trade("-10", at=NOW), NOW, CONFIG)

        assert state.equity == D(9_900)
