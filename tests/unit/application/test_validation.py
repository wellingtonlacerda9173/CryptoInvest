import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from engine.application.backtest import BacktestSettings, ReportMeta
from engine.application.regime import Regime, RegimeConfig, RegimeTimeline
from engine.application.selector import SelectorConfig, TradeSample
from engine.application.strategies import EmaCrossTrend
from engine.application.validation import (
    BuyHold,
    NotEnoughHistoryError,
    RandomReference,
    TradeStats,
    ValidationConfig,
    Verdict,
    buy_and_hold,
    corrected_alpha,
    judge,
    random_reference,
    run_validation,
    selector_walk_forward,
    trade_stats,
    validate_samples,
    window_bounds,
    write_validation_report,
)
from engine.domain.candle import Candle
from engine.domain.types import Timeframe
from tests.synthetic import synthetic_candles

START = datetime(2025, 1, 1, tzinfo=UTC)
CFG = ValidationConfig(train_days_min=120, test_days=60, simulations=200)


def candles(days: int = 360, timeframe: Timeframe = Timeframe.H4) -> list[Candle]:
    return synthetic_candles("BTC/USDT", timeframe, START, days * (86_400 // timeframe.seconds))


def trade(
    strategy: str, day: float, r: float, regime: Regime = Regime.TENDENCIA_ALTA, operable: bool = True
) -> TradeSample:
    opened = START + timedelta(days=day)
    return TradeSample(
        strategy=strategy,
        symbol="BTC/USDT",
        regime=regime,
        operable=operable,
        opened_at=opened,
        closed_at=opened + timedelta(hours=8),
        r_multiple=r,
        return_frac=r * 0.005,
        stop_pct=0.01,
        target_pct=0.02,
    )


def history(
    strategy: str, pattern: tuple[float, float], every: float = 2.0, until: int = 360
) -> list[TradeSample]:
    return [trade(strategy, 1 + i * every, pattern[i % 2]) for i in range(int(until / every) - 1)]


GOOD, BAD = (1.5, -0.5), (-1.0, 0.5)


def decisions_of(windows: list, index: int) -> list:  # type: ignore[type-arg]
    return [d.model_dump() for d in windows[index].decisions]


def test_window_bounds_and_short_history() -> None:
    bounds = window_bounds(candles(), CFG)
    assert len(bounds) == 4 and bounds[0][0] == 120 * 6 and all(e - s == 60 * 6 for s, e in bounds)
    with pytest.raises(NotEnoughHistoryError):
        window_bounds(candles(100), CFG)


def test_selector_picks_good_strategy_and_stays_out_of_bad_one() -> None:
    windows, picked, solo, _ = selector_walk_forward(
        candles(), history("A", GOOD) + history("B", BAD), ["A", "B"], CFG
    )
    assert all(
        d.chosen_label == "A" for w in windows for d in w.decisions if d.regime is Regime.TENDENCIA_ALTA
    )
    assert {t.strategy for t in picked} == {"A"}
    assert len(solo["B"]) > 0
    assert all(d.chosen_label == "nenhuma" for d in windows[0].decisions if d.regime is Regime.LATERAL)


def test_idle_selector_counts_as_zero_and_is_never_valid() -> None:
    few = [trade("A", 1 + i * 20, 1.5) for i in range(17)]
    report = validate_samples(candles(), few, ["A"], CFG)
    assert all(w.selector_trades == 0 and w.idle_regimes == 3 for w in report.windows)
    assert report.selector.stats.trades == 0
    assert report.selector.judgement.verdict is Verdict.AMOSTRA_INSUFICIENTE
    assert report.strategies[0].judgement.verdict is Verdict.AMOSTRA_INSUFICIENTE


def test_trades_in_non_operable_regimes_are_not_taken() -> None:
    samples = history("A", GOOD) + [
        trade("A", 130 + i, 9.0, Regime.ALTA_VOLATILIDADE, operable=False) for i in range(20)
    ]
    _, picked, _, _ = selector_walk_forward(candles(), samples, ["A"], CFG)
    assert all(t.r_multiple != 9.0 for t in picked)


def test_no_leakage_future_changes_do_not_alter_past_decisions() -> None:
    base = history("A", GOOD) + history("B", BAD)
    cut = 200
    altered = [
        s.model_copy(update={"r_multiple": -5.0 if s.strategy == "A" else 5.0})
        if s.opened_at > START + timedelta(days=cut)
        else s
        for s in base
    ]
    windows_a, *_ = selector_walk_forward(candles(), base, ["A", "B"], CFG)
    windows_b, *_ = selector_walk_forward(candles(), altered, ["A", "B"], CFG)
    for index, window in enumerate(windows_a):
        if window.as_of <= START + timedelta(days=cut):
            assert decisions_of(windows_a, index) == decisions_of(windows_b, index)
    assert decisions_of(windows_a, 3) != decisions_of(windows_b, 3)


def test_no_leakage_candles_after_a_moment_do_not_change_regimes_known_before() -> None:
    series = candles()
    altered = series[:1800] + [
        c.model_copy(update={"open": c.open * 2, "high": c.high * 2, "low": c.low * 2, "close": c.close * 2})
        for c in series[1800:]
    ]
    a, b = RegimeTimeline.from_candles(series), RegimeTimeline.from_candles(altered)
    for moment in (series[i].close_time for i in range(0, 1800, 37)):
        assert a.at(moment) == b.at(moment)


def test_corrected_alpha_sidak_and_bonferroni() -> None:
    assert corrected_alpha(0.05, 1, "sidak") == pytest.approx(0.05)
    assert corrected_alpha(0.05, 10, "sidak") == pytest.approx(1 - 0.95 ** (1 / 10))
    assert corrected_alpha(0.05, 10, "bonferroni") == pytest.approx(0.005)
    assert corrected_alpha(0.05, 10, "sidak") > corrected_alpha(0.05, 10, "bonferroni")
    assert corrected_alpha(0.05, 20, "sidak") < corrected_alpha(0.05, 5, "sidak")


def stats(trades: int = 60, mean_r: float = 0.4, total_return: float = 0.06) -> TradeStats:
    return TradeStats(
        trades=trades, mean_r=mean_r, total_r=mean_r * trades, max_drawdown_r=2.0,
        total_return=total_return, max_drawdown=0.01, win_rate=0.5,
    )  # fmt: skip


def reference(p_better: float, p_worse: float, sims: int = 1000) -> RandomReference:
    return RandomReference(
        simulations=sims, seed=1, mean_r_p05=-0.5, mean_r_median=-0.2, mean_r_p95=0.1,
        real_mean_r=0.4, p_better=p_better, p_worse=p_worse,
    )  # fmt: skip


BH_WEAK = BuyHold(total_return=0.02, max_drawdown=0.2, candles=100)
BH_STRONG = BuyHold(total_return=0.50, max_drawdown=0.01, candles=100)


def verdict_of(
    s: TradeStats, ref: RandomReference | None, bh: BuyHold = BH_WEAK, comparisons: int = 3
) -> Verdict:
    return judge(s, ref, bh, CFG, comparisons=comparisons, min_trades=30).verdict


def test_verdicts() -> None:
    assert verdict_of(stats(), reference(0.001, 1.0)) is Verdict.SUPERA
    assert verdict_of(stats(), reference(0.4, 0.7)) is Verdict.NAO_DISTINGUIVEL
    assert verdict_of(stats(mean_r=-0.5, total_return=-0.05), reference(1.0, 0.002)) is Verdict.PIOR
    assert verdict_of(stats(), None) is Verdict.NAO_DISTINGUIVEL


def test_small_sample_is_never_valid_even_with_perfect_evidence() -> None:
    for trades in (0, 1, 29):
        result = judge(
            stats(trades=trades), reference(0.001, 1.0), BH_WEAK, CFG, comparisons=1, min_trades=30
        )
        assert result.verdict is Verdict.AMOSTRA_INSUFICIENTE and not result.valid
    assert judge(stats(trades=30), reference(0.001, 1.0), BH_WEAK, CFG, comparisons=1, min_trades=30).valid


def test_multiple_comparisons_make_the_criterion_stricter() -> None:
    ref = reference(0.03, 1.0)
    assert verdict_of(stats(), ref, comparisons=1) is Verdict.SUPERA
    assert verdict_of(stats(), ref, comparisons=8) is Verdict.NAO_DISTINGUIVEL
    result = judge(stats(), ref, BH_WEAK, CFG, comparisons=8, min_trades=30)
    assert result.comparisons == 8 and result.alpha_corrected < result.alpha


def test_must_also_beat_buy_and_hold() -> None:
    result = judge(stats(), reference(0.001, 1.0), BH_STRONG, CFG, comparisons=1, min_trades=30)
    assert result.verdict is Verdict.NAO_DISTINGUIVEL and result.beats_buy_hold is False
    assert any("comprar-e-segurar" in n for n in result.notes)


def test_buy_and_hold_known_values() -> None:
    series = candles(30, Timeframe.D1)
    first, last = series[0], series[9]
    result = buy_and_hold(series, [(0, 10)])
    assert result.total_return == pytest.approx(float(last.close / first.open) - 1)
    assert 0 <= result.max_drawdown < 1 and result.candles == 10


def test_random_reference_is_deterministic_and_seed_sensitive() -> None:
    series = candles()
    bounds = window_bounds(series, CFG)
    real = [trade("A", 125 + i * 3, 0.5 if i % 2 else -1.0) for i in range(40)]
    first, again = random_reference(series, bounds, real, CFG), random_reference(series, bounds, real, CFG)
    other = random_reference(series, bounds, real, CFG.model_copy(update={"seed": 7}))
    assert first == again
    assert first is not None and other is not None and first.mean_r_median != other.mean_r_median


def test_random_reference_is_coherent_with_matching_distribution() -> None:
    series = candles()
    bounds = window_bounds(series, CFG)
    real = [trade("A", 125 + i * 3, 0.0) for i in range(40)]
    ref = random_reference(series, bounds, real, CFG)
    assert ref is not None
    assert ref.mean_r_p05 <= ref.mean_r_median <= ref.mean_r_p95
    assert ref.p_better + ref.p_worse >= 1.0
    assert 0 < ref.p_better <= 1 and 0 < ref.p_worse <= 1
    # real ficou na mediana do acaso => nenhum dos dois lados e significativo
    centered = real[0].model_copy(update={"r_multiple": ref.mean_r_median})
    middle = random_reference(series, bounds, [centered], CFG.model_copy(update={"seed": 3}))
    assert middle is not None and middle.p_better > 0.05 and middle.p_worse > 0.05


def test_random_reference_ignores_trades_without_levels() -> None:
    series = candles()
    bounds = window_bounds(series, CFG)
    blind = trade("A", 130, 1.0).model_copy(update={"stop_pct": None})
    assert random_reference(series, bounds, [blind], CFG) is None


def test_trade_stats() -> None:
    result = trade_stats([trade("A", 1, 1.0), trade("A", 2, -2.0), trade("A", 3, 1.0)])
    assert result.trades == 3 and result.total_r == 0 and result.max_drawdown_r == 2.0
    assert result.win_rate == pytest.approx(2 / 3)
    assert trade_stats([]).mean_r is None


@given(
    rs=st.lists(st.floats(-3, 3, allow_nan=False), min_size=0, max_size=29),
    p=st.floats(0.0005, 1),
)
@settings(max_examples=40, deadline=None)
def test_property_below_minimum_never_valid(rs: list[float], p: float) -> None:
    s = trade_stats([trade("A", 1 + i, r) for i, r in enumerate(rs)])
    result = judge(s, reference(p, 1 - p + 0.0005), BH_WEAK, CFG, comparisons=1, min_trades=30)
    assert not result.valid


async def test_full_report_end_to_end_with_real_engine_and_serialization(tmp_path: Path) -> None:
    series = candles(250)
    config = ValidationConfig(
        train_days_min=120, test_days=60, simulations=100,
        selector=SelectorConfig(min_trades=5, min_effective_trades=3),
        regime=RegimeConfig(),
    )  # fmt: skip
    fixed = datetime(2026, 1, 1, tzinfo=UTC)
    result = await run_validation(series, [EmaCrossTrend()], BacktestSettings(), config, clock=lambda: fixed)
    assert result.comparisons == 2 and result.generated_at == fixed
    assert math.isfinite(result.buy_hold.total_return)
    paths = write_validation_report(tmp_path, result, ReportMeta(name="teste"))
    payload = json.loads(paths[0].read_text(encoding="utf-8"))
    assert payload["report"]["comparisons"] == 2
    assert "Comparações testadas: 2" in paths[1].read_text(encoding="utf-8")
