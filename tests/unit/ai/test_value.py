from datetime import UTC, datetime, timedelta
from decimal import Decimal

from engine.application.ai.value import (
    ApprovedTrade,
    VetoInput,
    build_report,
    simulate_vetoes,
)
from engine.application.backtest.settings import BacktestSettings
from engine.domain.candle import Candle
from engine.domain.signal import Signal
from engine.domain.types import Timeframe

START = datetime(2026, 10, 1, tzinfo=UTC)
END = START + timedelta(days=30)
SETTINGS = BacktestSettings()


def candle(i: int, o: str, h: str, low: str, c: str) -> Candle:
    return Candle(
        symbol="BTC/USDT",
        timeframe=Timeframe.H1,
        open_time=START + timedelta(hours=i),
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(low),
        close=Decimal(c),
        volume=Decimal(10),
        closed=True,
    )


def veto(path: list[Candle], equity: str | None = "10000", code: str = "ai_veto") -> VetoInput:
    signal = Signal(
        symbol="BTC/USDT",
        timeframe=Timeframe.H1,
        created_at=path[0].close_time,
        strategy="elephant",
        entry=Decimal(100),
        stop_loss=Decimal(98),
        take_profit=Decimal(104),
    )
    return VetoInput(
        decided_at=signal.created_at,
        code=code,
        strategy="elephant",
        symbol="BTC/USDT",
        signal=signal,
        equity=None if equity is None else Decimal(equity),
        candles=tuple(path),
    )


def hits_stop() -> VetoInput:
    return veto(
        [
            candle(0, "100", "101", "99", "100"),
            candle(1, "100", "100.5", "99.5", "100"),
            candle(2, "100", "100", "96", "97"),
        ]
    )


def hits_target() -> VetoInput:
    return veto(
        [
            candle(0, "100", "101", "99", "100"),
            candle(1, "100", "100.5", "99.5", "100"),
            candle(2, "100", "106", "100", "105"),
        ]
    )


def never_fills() -> VetoInput:
    flat = [candle(i, "110", "111", "109", "110") for i in range(8)]
    return veto(flat)


def report(
    vetoes: list[VetoInput], approved: list[ApprovedTrade] | None = None, calls: int = 3, cost: float = 0.05
):  # type: ignore[no-untyped-def]
    return build_report(
        simulate_vetoes(vetoes, SETTINGS),
        approved or [],
        calls=calls,
        failed_calls=1,
        cost_usd=cost,
        start=START,
        end=END,
    )


def test_known_cases_stop_is_avoided_loss_and_target_is_a_false_veto() -> None:
    result = report([hits_stop(), hits_target(), never_fills()])
    assert result.vetoes == 3 and result.simulated == 2 and result.not_filled == 1
    assert result.correct_vetoes == 1 and result.false_vetoes == 1 and result.false_veto_rate == 0.5
    assert result.avoided_loss > 0 and result.missed_gain > 0
    assert result.net_avoided == round(result.avoided_loss - result.missed_gain, 2)
    assert result.net_value_usd == round(result.net_avoided - 0.05, 2)
    assert {c.status for c in result.cases} == {"stop", "target"}


def test_small_sample_is_never_conclusive() -> None:
    result = report([hits_stop()] * 5)
    assert result.verdict == "insufficient_sample" and "Amostra insuficiente" in result.verdict_text
    assert report([hits_stop()] * 15).verdict == "no_conclusion"


def test_large_sample_of_losing_vetoes_concludes_the_ai_avoided_losses() -> None:
    stops = [hits_stop(), hits_stop(), hits_stop()] * 10
    assert report(stops).verdict == "ai_avoided_losses"
    assert report([hits_target(), hits_target(), hits_target()] * 10).verdict == "ai_missed_gains"
    mixed = [hits_stop(), hits_target()] * 15
    assert report(mixed).verdict in {"no_difference", "ai_avoided_losses", "ai_missed_gains"}


def test_missing_equity_or_candles_is_not_simulated_not_invented() -> None:
    result = report([veto(hits_stop().candles, equity=None)])
    assert result.not_simulated == 1 and result.simulated == 0 and result.avoided_loss == 0


def test_no_ai_activity_is_an_explanatory_empty_report() -> None:
    result = report([], calls=0, cost=0.0)
    assert result.verdict == "no_activity" and "Sem atividade" in result.verdict_text and result.cases == ()


def test_approved_losses_are_always_reported() -> None:
    approved = [
        ApprovedTrade(strategy="elephant", symbol="BTC/USDT", pnl=-40.0, r_multiple=-1.0),
        ApprovedTrade(strategy="elephant", symbol="BTC/USDT", pnl=80.0, r_multiple=2.0),
        ApprovedTrade(strategy="ema", symbol="ETH/USDT", pnl=-30.0, r_multiple=-0.8),
    ]
    result = report([hits_stop()], approved)
    assert (result.approved_trades, result.approved_wins, result.approved_losses) == (3, 1, 2)
    assert (
        result.approved_loss_pnl == -70.0 and result.approved_pnl == 10.0 and result.approved_total_r == 0.2
    )
