from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from engine.application.backtest import BacktestSettings
from engine.application.risk import AuditAction, AuditEntry, RiskState
from engine.application.validation import (
    BuyHold,
    RandomReference,
    TradeStats,
    ValidationConfig,
    Verdict,
    judge,
)
from engine.application.validation.idle import fraction_of, lock_spans

T0 = datetime(2026, 1, 1, tzinfo=UTC)
STATS = TradeStats(
    trades=60, mean_r=0.4, total_r=24, max_drawdown_r=2.0, total_return=0.06, max_drawdown=0.01, win_rate=0.5
)
BH = BuyHold(total_return=0.02, max_drawdown=0.2, candles=100)


def ref(sims: int, p_better: float) -> RandomReference:
    return RandomReference(
        simulations=sims, seed=1, mean_r_p05=-0.5, mean_r_median=-0.2, mean_r_p95=0.1,
        real_mean_r=0.4, p_better=p_better, p_worse=1.0,
    )  # fmt: skip


def verdict(reference: RandomReference, m: int, idle: float | None = None) -> Verdict:
    return judge(
        STATS, reference, BH, ValidationConfig(), comparisons=m, min_trades=30, idle_fraction=idle
    ).verdict


def test_underpowered_test_never_emits_supera() -> None:
    result = judge(STATS, ref(1000, 1 / 1001), BH, ValidationConfig(), comparisons=78, min_trades=30)
    assert result.underpowered and result.min_attainable_p == pytest.approx(1 / 1001)
    assert result.verdict is Verdict.NAO_DISTINGUIVEL and any("sem poder" in n for n in result.notes)


def test_powered_test_can_emit_supera() -> None:
    result = judge(STATS, ref(10_000, 1 / 10_001), BH, ValidationConfig(), comparisons=78, min_trades=30)
    assert not result.underpowered and result.verdict is Verdict.SUPERA


def test_strategy_idle_for_too_long_is_censored_not_valid() -> None:
    assert verdict(ref(10_000, 1 / 10_001), 78, idle=0.31) is Verdict.CENSURADO
    assert verdict(ref(10_000, 1 / 10_001), 78, idle=0.30) is Verdict.SUPERA


def test_lock_spans_cover_pauses_and_clip_to_windows() -> None:
    events = [
        AuditEntry(at=T0 + timedelta(hours=6), action=AuditAction.DAILY_TARGET_REACHED, detail="x"),
        AuditEntry(
            at=T0 + timedelta(days=2),
            action=AuditAction.PAIR_PAUSED,
            detail=f"BTC/USDT: perdas; pausa ate {(T0 + timedelta(days=3)).isoformat()}",
        ),
    ]
    spans = lock_spans(events, T0 + timedelta(days=10))
    assert spans == [
        (T0 + timedelta(hours=6), T0 + timedelta(days=1)),
        (T0 + timedelta(days=2), T0 + timedelta(days=3)),
    ]
    window = [(T0, T0 + timedelta(days=4))]
    assert fraction_of(spans, window) == pytest.approx((18 / 24 + 1) / 4)


def test_drawdown_halt_is_off_only_in_the_state_risk_of_the_collection_settings() -> None:
    state = RiskState.initial(Decimal(10_000), T0).model_copy(update={"equity": Decimal(9_000)})
    production = BacktestSettings()
    collection = production.model_copy(update={"drawdown_halt": False})
    assert state.observe_equity(Decimal(9_000), T0, production.state_risk()).halted
    assert not state.observe_equity(Decimal(9_000), T0, collection.state_risk()).halted
    assert collection.risk == production.risk and production.risk.max_drawdown == Decimal("0.05")
