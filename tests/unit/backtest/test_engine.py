from datetime import timedelta
from decimal import Decimal
from typing import ClassVar

import pytest

from engine.application.backtest import (
    BacktestEngine,
    DecisionAction,
    HypotheticalStatus,
    summarize_avoided,
)
from engine.application.backtest.models import BacktestResult
from engine.application.risk import AuditAction
from engine.application.risk.sizing import floor_to_step
from engine.domain.trade import ExitReason
from tests.unit.backtest.engine_helpers import (
    ENTRY_CANDLE,
    FIXED_NOW,
    QUIET,
    T0,
    Scripted,
    flat_then,
    mk,
    run,
    settings,
)

D = Decimal
FEE = D("0.001")
SLIP = D("0.0005")
LOSS = [ENTRY_CANDLE, ("100.5", "101", "94", "95")]
WIN = [
    ENTRY_CANDLE,
    ("100.5", "112", "100", "111.5"),
    ("111.5", "120", "111", "119"),
    ("119", "119", "106", "107"),
]


def codes(result: BacktestResult) -> list[str]:
    return [d.code for d in result.decisions]


def assert_ledger_matches_cash(result: BacktestResult) -> None:
    assert result.final_realized_equity == D(10_000) + sum((t.pnl for t in result.trades), D(0))


class TestWinningTrade:
    @pytest.fixture
    async def result(self) -> BacktestResult:
        # alvo 111 toca na 2a vela (parcial), trailing sobe para 115 (maxima 120 - 1R), stop em 115 na 4a
        return await run(flat_then([*WIN, *[QUIET] * 3]))

    async def test_partial_at_target_then_trailing_stop(self, result: BacktestResult) -> None:
        partial, final = result.trades
        assert (partial.exit_reason, partial.exit_price) == (ExitReason.TAKE_PROFIT, D(111))
        assert (final.exit_reason, final.exit_price) == (ExitReason.STOP_LOSS, D(115) * (1 - SLIP))
        assert partial.quantity + final.quantity == result.round_trips[0].quantity
        assert partial.quantity == D("4.81231")

    async def test_fees_come_from_the_gateway_fills(self, result: BacktestResult) -> None:
        partial, final = result.trades
        assert partial.exit_fee == partial.quantity * D(111) * FEE
        assert final.exit_fee == final.quantity * final.exit_price * FEE
        assert partial.entry_fee + final.entry_fee == result.round_trips[0].quantity * D(100) * FEE

    async def test_round_trip_aggregates_legs_and_r_multiple(self, result: BacktestResult) -> None:
        [trip] = result.round_trips
        assert trip.legs == 2
        assert trip.pnl == sum((t.pnl for t in result.trades), D(0))
        assert trip.r_multiple == trip.pnl / trip.risk_amount
        assert trip.exit_reasons == (ExitReason.TAKE_PROFIT, ExitReason.STOP_LOSS)
        assert trip.r_multiple > 2

    async def test_capital_reconciles_with_gateway_cash(self, result: BacktestResult) -> None:
        assert_ledger_matches_cash(result)
        assert result.final_equity == result.final_realized_equity

    async def test_daily_target_locks_new_entries(self, result: BacktestResult) -> None:
        assert AuditAction.DAILY_TARGET_REACHED in {e.action for e in result.risk_events}
        again = await run(flat_then([*WIN, *[QUIET] * 4]), at={29, 36})
        assert codes(again) == ["approved", "daily_target_reached"]


class TestLosingTrades:
    async def test_stop_loss_exits_everything_and_is_a_loss(self) -> None:
        result = await run(flat_then([*LOSS, QUIET, QUIET]))

        [trade] = result.trades
        price = D(95) * (1 - SLIP)
        assert (trade.exit_reason, trade.exit_price) == (ExitReason.STOP_LOSS, price)
        quantity = trade.quantity
        expected = (price - 100) * quantity - quantity * 100 * FEE - quantity * price * FEE
        assert trade.pnl == expected
        assert trade.pnl < 0
        assert result.round_trips[0].r_multiple < -1
        assert_ledger_matches_cash(result)

    async def test_gap_below_stop_fills_at_the_open_not_at_the_stop(self) -> None:
        result = await run(flat_then([ENTRY_CANDLE, ("90", "91", "89", "90"), QUIET]))

        [trade] = result.trades
        assert trade.exit_price == D(90) * (1 - SLIP)
        assert result.round_trips[0].r_multiple < D("-1.9")

    async def test_stop_on_the_entry_candle_never_breaks_trade_chronology(self) -> None:
        result = await run(flat_then([("90", "101", "89", "100"), QUIET]))

        [trade] = result.trades
        assert trade.closed_at >= trade.opened_at
        assert trade.exit_reason is ExitReason.STOP_LOSS

    async def test_two_losses_pause_the_pair_for_24h(self) -> None:
        rows = [*LOSS, QUIET, *LOSS, QUIET, QUIET]
        result = await run(flat_then(rows), at={29, 32, 35}, max_daily_loss=D("0.05"))

        assert codes(result) == ["approved", "approved", "pair_paused"]
        assert [r.pnl < 0 for r in result.round_trips] == [True, True]
        assert AuditAction.PAIR_PAUSED in {e.action for e in result.risk_events}

    async def test_daily_loss_pauses_the_day_and_next_utc_day_resumes(self) -> None:
        to_midnight = 24 - 30 % 24
        rows = [*LOSS, *[QUIET] * (to_midnight + 4)]
        result = await run(
            flat_then(rows),
            at={29, 33, 30 + to_midnight + 2},
            risk_per_trade=D("0.01"),
            max_daily_loss=D("0.01"),
        )

        assert codes(result) == ["approved", "daily_loss_pause", "approved"]
        assert AuditAction.DAILY_LOSS_PAUSE in {e.action for e in result.risk_events}

    async def test_drawdown_limit_halts_the_robot_for_good(self) -> None:
        rows = [*LOSS, QUIET, *LOSS, QUIET, QUIET]
        result = await run(flat_then(rows), at={29, 32, 35}, max_drawdown=D("0.01"), max_daily_loss=D("0.05"))

        assert result.halted
        assert codes(result)[-1] == "robot_halted"
        assert AuditAction.HALTED in {e.action for e in result.risk_events}


class TestProfitProtection:
    async def test_giveback_sells_at_market_on_the_next_open(self) -> None:
        rows = [ENTRY_CANDLE, ("100.5", "106", "100", "103"), ("102", "103", "101", "102"), QUIET]
        result = await run(flat_then(rows))

        [trade] = result.trades
        assert trade.exit_reason is ExitReason.PROFIT_PROTECTION
        assert trade.exit_price == D(102) * (1 - SLIP)
        assert trade.exit_fee == trade.quantity * trade.exit_price * FEE
        assert trade.closed_at == T0 + timedelta(hours=32)
        assert_ledger_matches_cash(result)


class TestEmergencyExit:
    async def test_fill_with_abnormal_fee_is_sold_at_market_immediately(self) -> None:
        result = await run(flat_then([QUIET] * 6), maker_fee_rate=D("0.0025"))

        assert "emergency_exit" in codes(result)
        [trade] = result.trades
        assert trade.exit_reason is ExitReason.MANUAL
        assert trade.exit_price == D(100) * (1 - SLIP)
        assert trade.pnl < 0
        assert result.round_trips[0].exit_reasons == (ExitReason.MANUAL,)
        assert_ledger_matches_cash(result)

    async def test_limit_that_could_only_execute_after_the_approval_expired_is_cancelled(self) -> None:
        result = await run(flat_then([QUIET] * 6), latency_candles=3)

        assert codes(result) == ["approved", "entry_expired"]
        assert result.trades == ()


class TestUnfilledEntries:
    async def test_limit_not_reached_expires_and_is_not_a_trade(self) -> None:
        result = await run(flat_then([("101", "102", "100.5", "101")] * 6))

        assert codes(result) == ["approved", "entry_expired"]
        assert result.trades == ()
        assert result.final_equity == D(10_000)
        assert all(d.hypothetical is None for d in result.decisions)


class TestDecisionsAndAvoidedTrades:
    NO_LIQUIDITY: ClassVar[dict[str, object]] = {"min_quote_volume_24h": D("1e12")}

    async def test_vetoed_signal_is_recorded_with_code_and_simulated_to_target(self) -> None:
        rows = [ENTRY_CANDLE, ("100.5", "112", "100", "111.5"), QUIET]
        result = await run(flat_then(rows), **self.NO_LIQUIDITY)

        [decision] = result.decisions
        assert (decision.action, decision.code) == (DecisionAction.SKIPPED, "low_liquidity")
        hypothetical = decision.hypothetical
        assert hypothetical is not None
        assert hypothetical.status is HypotheticalStatus.TARGET
        risk_amount = D(10_000) * D("0.005")
        quantity = floor_to_step(risk_amount / (D(5) + FEE * D(195)), D("0.00001"))
        assert hypothetical.pnl == quantity * (D(11) - D(100) * FEE - D(111) * FEE)
        assert hypothetical.r_multiple == hypothetical.pnl / (quantity * (D(5) + FEE * D(195)))
        assert result.final_equity == D(10_000)

    async def test_avoided_loss_and_forgone_gain_are_kept_separate(self) -> None:
        rows = [*LOSS, QUIET, QUIET, ENTRY_CANDLE, ("100.5", "112", "100", "111.5"), QUIET]
        result = await run(flat_then(rows), at={29, 33}, **self.NO_LIQUIDITY)

        summary = summarize_avoided(result.decisions)
        assert summary.signals == summary.simulated == 2
        assert summary.avoided_losses > 0
        assert summary.forgone_gains > 0
        assert summary.hypothetical_pnl == summary.forgone_gains - summary.avoided_losses
        assert summary.net_effect == summary.avoided_losses - summary.forgone_gains
        assert summary.by_code[0].code == "low_liquidity"

    async def test_hypothetical_not_filled_when_limit_is_never_touched(self) -> None:
        result = await run(flat_then([("101", "102", "100.5", "101")] * 5), **self.NO_LIQUIDITY)

        hypothetical = result.decisions[0].hypothetical
        assert hypothetical is not None
        assert hypothetical.status is HypotheticalStatus.NOT_FILLED
        assert summarize_avoided(result.decisions).simulated == 0

    async def test_evaluations_without_signal_are_counted_not_listed(self) -> None:
        result = await run(flat_then([QUIET] * 4), at=set())

        assert result.decisions == ()
        assert result.steps_without_signal == 33


class TestDeterminismAndLookahead:
    async def test_same_input_same_result(self) -> None:
        candles = flat_then([*WIN, QUIET])

        assert await run(candles) == await run(candles)

    async def test_strategy_only_sees_closed_candles_up_to_now(self) -> None:
        candles = flat_then([QUIET] * 10)
        strategy = Scripted(set())
        await BacktestEngine([strategy], settings(), clock=lambda: FIXED_NOW).run(candles)

        assert [seen for _, seen in strategy.seen] == [c.close_time for c in candles[:-1]]
        assert max(length for length, _ in strategy.seen) <= 60

    async def test_changing_future_candles_does_not_change_past_decisions(self) -> None:
        past = [*LOSS, QUIET, QUIET]
        a = flat_then([*past, ("100", "130", "100", "129"), ("129", "140", "128", "139")])
        b = flat_then([*past, ("100", "100.1", "50", "55"), ("55", "56", "40", "41")])
        cut = a[30 + len(past) - 1].close_time

        ra, rb = await run(a, at={29, 33}), await run(b, at={29, 33})

        assert [d for d in ra.decisions if d.at <= cut] == [d for d in rb.decisions if d.at <= cut]
        assert [p for p in ra.equity_curve if p.at <= cut] == [p for p in rb.equity_curve if p.at <= cut]
        assert [t for t in ra.trades if t.closed_at <= cut] == [t for t in rb.trades if t.closed_at <= cut]

    async def test_warmup_candles_are_not_part_of_the_backtest(self) -> None:
        candles = flat_then([ENTRY_CANDLE, QUIET, QUIET], warmup=40)
        engine = BacktestEngine([Scripted({39})], settings(), clock=lambda: FIXED_NOW)
        result = await engine.run(candles, start=35)

        assert result.candles == len(candles) - 35
        assert result.equity_curve[0].at == mk(35, "1", "1", "1", "1").close_time


class TestPositionTooSmallToSplit:
    async def test_whole_position_trails_when_partial_would_break_exchange_minimums(self) -> None:
        rows = [
            ENTRY_CANDLE,
            ("100.5", "112", "100", "111.5"),
            ("111.5", "120", "111", "119"),
            ("119", "119", "106", "107"),
            QUIET,
        ]
        result = await run(flat_then(rows), initial_capital=D(60))

        [trade] = result.trades
        assert (trade.exit_reason, trade.exit_price) == (ExitReason.STOP_LOSS, D(115) * (1 - SLIP))
        assert trade.quantity == result.round_trips[0].quantity
        assert result.round_trips[0].legs == 1
