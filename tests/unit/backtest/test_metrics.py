from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from engine.application.backtest import metrics_from_parts
from engine.application.backtest.metrics import max_drawdown, sharpe_ratio
from engine.application.backtest.models import EquityPoint, RoundTrip
from engine.domain.trade import ExitReason

D = Decimal
INITIAL = D(1000)
JAN_30 = datetime(2026, 1, 30, tzinfo=UTC)


def curve(equities: list[int], in_position: list[bool] | None = None) -> list[EquityPoint]:
    flags = in_position or [False] * len(equities)
    return [
        EquityPoint(at=JAN_30 + timedelta(days=i, hours=12), equity=D(e), realized=D(e), in_position=flag)
        for i, (e, flag) in enumerate(zip(equities, flags, strict=True))
    ]


def trip(pnl: int, strategy: str = "a", day: int = 0) -> RoundTrip:
    closed = JAN_30 + timedelta(days=day)
    return RoundTrip(
        order_id=f"o{pnl}{day}",
        symbol="BTC/USDT",
        strategy=strategy,
        opened_at=closed - timedelta(hours=2),
        closed_at=closed,
        entry_price=D(100),
        quantity=D(1),
        pnl=D(pnl),
        fees=D(1),
        risk_amount=D(100),
        r_multiple=D(pnl) / 100,
        exit_reasons=(ExitReason.STOP_LOSS if pnl < 0 else ExitReason.TAKE_PROFIT,),
        legs=1,
    )


# equity diario de 30/jan a 03/fev; capital inicial 1000
EQUITY = [1100, 1210, 1089, 1000, 1100]
TRIPS = [trip(200, "a", 0), trip(-100, "a", 1), trip(50, "a", 2), trip(-100, "b", 3), trip(-100, "b", 4)]


class TestDrawdownAndReturn:
    def test_max_drawdown_is_peak_to_trough_of_the_marked_curve(self) -> None:
        drawdown, days = max_drawdown(INITIAL, curve(EQUITY))

        assert drawdown == pytest.approx(210 / 1210, abs=1e-12)
        assert days == 3.0

    def test_initial_capital_is_the_first_peak(self) -> None:
        drawdown, _ = max_drawdown(INITIAL, curve([900, 950, 1000]))

        assert drawdown == pytest.approx(0.10)

    def test_no_drawdown_when_equity_only_rises(self) -> None:
        assert max_drawdown(INITIAL, curve([1010, 1020]))[0] == 0.0

    def test_total_return_and_pnl_use_the_last_marked_equity(self) -> None:
        m = metrics_from_parts(INITIAL, curve(EQUITY), TRIPS)

        assert m.total_return == pytest.approx(0.10)
        assert m.total_pnl == D(100)
        assert m.final_equity == D(1100)


class TestTradeStatistics:
    @pytest.fixture
    def m(self):  # type: ignore[no-untyped-def]
        return metrics_from_parts(INITIAL, curve(EQUITY, [False, True, True, False, False]), TRIPS)

    def test_win_rate_profit_factor_payoff_and_expectancy(self, m) -> None:  # type: ignore[no-untyped-def]
        assert (m.trades, m.wins, m.losses, m.breakeven) == (5, 2, 3, 0)
        assert m.win_rate == pytest.approx(2 / 5)
        assert m.profit_factor == pytest.approx(250 / 300)
        assert m.payoff == pytest.approx(125 / 100)
        assert m.expectancy_r == pytest.approx((2 - 1 + 0.5 - 1 - 1) / 5)

    def test_idle_time_is_the_share_of_candles_without_position(self, m) -> None:  # type: ignore[no-untyped-def]
        assert m.idle_pct == pytest.approx(3 / 5)

    def test_fees_are_summed(self, m) -> None:  # type: ignore[no-untyped-def]
        assert m.fees_paid == D(5)

    def test_by_strategy(self, m) -> None:  # type: ignore[no-untyped-def]
        a, b = m.by_strategy
        assert (a.name, a.trades, a.pnl) == ("a", 3, D(150))
        assert a.win_rate == pytest.approx(2 / 3)
        assert a.profit_factor == pytest.approx(250 / 100)
        assert a.expectancy_r == pytest.approx(0.5)
        assert (b.name, b.trades, b.pnl, b.win_rate, b.profit_factor, b.expectancy_r) == (
            "b",
            2,
            D(-200),
            0.0,
            0.0,
            -1.0,
        )

    def test_without_losses_ratios_are_undefined_not_infinite(self) -> None:
        m = metrics_from_parts(INITIAL, curve([1100]), [trip(100)])

        assert m.profit_factor is None
        assert m.payoff is None
        assert m.win_rate == 1.0

    def test_without_trades_the_robot_is_idle_and_ratios_are_undefined(self) -> None:
        m = metrics_from_parts(INITIAL, curve([1000, 1000, 1000]), [])

        assert (m.trades, m.win_rate, m.profit_factor, m.expectancy_r, m.idle_pct) == (
            0,
            None,
            None,
            None,
            1.0,
        )
        assert m.by_strategy == ()
        assert m.max_drawdown == 0.0

    def test_breakeven_is_neither_win_nor_loss(self) -> None:
        m = metrics_from_parts(INITIAL, curve([1000]), [trip(0)])

        assert (m.wins, m.losses, m.breakeven) == (0, 0, 1)


class TestMonthlyAndSharpe:
    def test_monthly_return_from_month_end_equity_and_realized_by_closing_month(self) -> None:
        m = metrics_from_parts(INITIAL, curve(EQUITY), TRIPS)

        january, february = m.monthly
        assert (january.month, february.month) == ("2026-01", "2026-02")
        assert january.return_pct == pytest.approx(0.21)
        assert february.return_pct == pytest.approx(1100 / 1210 - 1)
        assert (january.realized_pnl, january.trades) == (D(200) - D(100), 2)
        assert (february.realized_pnl, february.trades) == (D(50) - D(200), 3)
        assert m.positive_months_ratio == 0.5
        assert m.worst_month_return == pytest.approx(1100 / 1210 - 1)

    def test_sharpe_uses_daily_returns_annualized_by_sqrt_365(self) -> None:
        m = metrics_from_parts(INITIAL, curve(EQUITY), TRIPS)

        # retornos diarios: 0.1, 0.1, -0.1, 1000/1089 - 1, 0.1 -> media 0.0236547291, desvio 0.1047395173
        assert m.sharpe == pytest.approx(4.314732171613516, abs=1e-9)

    def test_sharpe_needs_two_days_and_some_variance(self) -> None:
        assert sharpe_ratio([0.01]) is None
        assert sharpe_ratio([0.01, 0.01, 0.01]) is None
        assert metrics_from_parts(INITIAL, curve([1000, 1000, 1000]), []).sharpe is None
