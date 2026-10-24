import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from engine.application.regime import Regime
from engine.application.selector import (
    SelectionDecision,
    SelectorConfig,
    StrategyScore,
    TradeSample,
    build_scores,
    compute_score,
    select_strategy,
)

AS_OF = datetime(2026, 6, 1, tzinfo=UTC)
CFG = SelectorConfig()


def sample(
    r: float,
    days_ago: float,
    strategy: str = "A",
    regime: Regime = Regime.TENDENCIA_ALTA,
    *,
    operable: bool = True,
) -> TradeSample:
    closed = AS_OF - timedelta(days=days_ago)
    return TradeSample(
        strategy=strategy,
        symbol="BTC/USDT",
        regime=regime,
        operable=operable,
        opened_at=closed - timedelta(hours=4),
        closed_at=closed,
        r_multiple=r,
    )


def series(
    rs: Sequence[float], strategy: str = "A", spacing: float = 2.0, regime: Regime = Regime.TENDENCIA_ALTA
) -> list[TradeSample]:
    """O ultimo valor e o trade mais recente."""
    count = len(rs)
    return [sample(r, (count - i) * spacing, strategy, regime) for i, r in enumerate(rs)]


def edge(count: int, strategy: str = "A", regime: Regime = Regime.TENDENCIA_ALTA) -> list[TradeSample]:
    return series([1.5 if i % 2 else -0.5 for i in range(count)], strategy, regime=regime)


def score_of(samples: Sequence[TradeSample], cfg: SelectorConfig = CFG) -> StrategyScore:
    return compute_score("A", Regime.TENDENCIA_ALTA, "BTC/USDT", samples, AS_OF, cfg)


def test_minimum_sample_boundary() -> None:
    below, at_min = score_of(edge(29)), score_of(edge(30))
    assert not below.eligible and "amostra insuficiente" in (below.ineligible_reason or "")
    assert at_min.eligible and at_min.ineligible_reason is None


def test_configurable_minimum_sample() -> None:
    cfg = SelectorConfig(min_trades=10, min_effective_trades=5)
    assert compute_score("A", Regime.TENDENCIA_ALTA, "BTC/USDT", edge(12), AS_OF, cfg).eligible


def test_larger_sample_has_tighter_conservative_score() -> None:
    small, large = score_of(edge(30)), score_of(edge(300))
    assert abs(small.mean_r - large.mean_r) < 0.1
    assert large.std_error < small.std_error
    assert large.score > small.score


def test_score_penalizes_drawdown_and_instability() -> None:
    flat = SelectorConfig(half_life_days=1e9)
    steady, clustered = series([1.0, -0.5] * 20), series([1.0] * 20 + [-0.5] * 20)
    a, b = score_of(steady, flat), score_of(clustered, flat)
    assert abs(a.mean_r - b.mean_r) < 1e-4
    assert b.max_drawdown_r > a.max_drawdown_r
    assert b.instability > a.instability
    assert b.score < a.score


def test_recent_results_weigh_more() -> None:
    old_good_recent_bad = series([1.0] * 20 + [-1.0] * 20, spacing=30)
    old_bad_recent_good = series([-1.0] * 20 + [1.0] * 20, spacing=30)
    assert score_of(old_bad_recent_good).mean_r > 0 > score_of(old_good_recent_bad).mean_r


def test_few_recent_trades_cannot_dominate() -> None:
    history = [sample(-0.5, 400 + i) for i in range(60)]
    recent = [sample(3.0, 0.1 * (i + 1)) for i in range(3)]
    result = score_of(history + recent)
    assert result.mean_r < 0
    assert result.effective_trades > 40


def test_future_trades_never_enter_the_score() -> None:
    base = edge(40)
    future = [sample(-9.0, -days) for days in (1, 5, 30)]
    assert score_of(base + future) == score_of(base)
    assert build_scores(base + future, AS_OF, CFG) == build_scores(base, AS_OF, CFG)


def test_scores_ignore_non_operable_regimes_and_group_by_key() -> None:
    lateral = series([1.0] * 40, regime=Regime.LATERAL)
    blind = [sample(5.0, d, regime=Regime.LATERAL, operable=False) for d in range(1, 40)]
    scores = build_scores(edge(40) + lateral + blind, AS_OF, CFG)
    assert set(scores) == {("A", "tendencia_alta", "BTC/USDT"), ("A", "lateral", "BTC/USDT")}
    assert scores[("A", "lateral", "BTC/USDT")].trades == 40


def decide(samples: Sequence[TradeSample], names: Sequence[str] = ("A", "B")) -> SelectionDecision:
    scores = build_scores(samples, AS_OF, CFG)
    return select_strategy(Regime.TENDENCIA_ALTA, "BTC/USDT", names, scores, CFG)


def test_picks_best_eligible_strategy() -> None:
    weak = series([1.0, -0.8] * 20, "B")
    decision = decide(edge(40) + weak)
    assert decision.operate and decision.chosen_label == "A"
    assert decision.weight_of("A") == 1.0
    assert "Operar com A" in decision.reason


def test_no_eligible_strategy_means_no_operation_with_reason() -> None:
    decision = decide(edge(29) + series([1.5, -0.5] * 5, "B"))
    assert not decision.operate and decision.chosen == () and decision.chosen_label == "nenhuma"
    assert decision.reason.startswith("Não operar")
    assert all(not e.eligible and e.reason for e in decision.evaluated)
    assert "amostra insuficiente" in decision.reason


def test_score_below_minimum_means_no_operation() -> None:
    mediocre = series([0.2, -0.1] * 25)
    decision = decide(mediocre, ("A",))
    assert not decision.operate
    assert "abaixo do mínimo" in decision.reason
    assert decision.evaluated[0].score is not None


def test_min_score_is_configurable() -> None:
    cfg = SelectorConfig(min_score=-10.0)
    scores = build_scores(series([0.2, -0.1] * 25), AS_OF, cfg)
    assert select_strategy(Regime.TENDENCIA_ALTA, "BTC/USDT", ["A"], scores, cfg).operate


def test_unknown_history_is_ineligible() -> None:
    decision = decide([], ("A",))
    assert not decision.operate and "sem histórico" in decision.reason


def test_non_operable_regime_never_operates_even_with_great_history() -> None:
    samples = series([1.5, -0.5] * 30, regime=Regime.ALTA_VOLATILIDADE)
    scores = build_scores(samples, AS_OF, CFG)
    decision = select_strategy(Regime.ALTA_VOLATILIDADE, "BTC/USDT", ["A"], scores, CFG, operable=False)
    assert not decision.operate
    assert decision.reason == "Não operar: regime alta volatilidade sem operação."
    assert all(e.reason == "regime sem operação" for e in decision.evaluated)
    lateral = select_strategy(
        Regime.LATERAL,
        "BTC/USDT",
        ["A"],
        build_scores(edge(40, regime=Regime.LATERAL), AS_OF, CFG),
        CFG,
        operable=False,
    )
    assert not lateral.operate


def test_weighted_combination_sums_to_one() -> None:
    cfg = SelectorConfig(max_combined=2, combine_margin=1.0)
    samples = edge(40) + series([1.2, -0.4] * 20, "B")
    scores = build_scores(samples, AS_OF, cfg)
    decision = select_strategy(Regime.TENDENCIA_ALTA, "BTC/USDT", ["A", "B"], scores, cfg)
    assert len(decision.chosen) == 2
    assert abs(sum(c.weight for c in decision.chosen) - 1.0) < 1e-9


def test_decision_context_is_json_serializable() -> None:
    context = decide(edge(40)).to_context()
    assert json.loads(json.dumps(context))["chosen_label"] == "A"


@given(
    rs=st.lists(st.floats(-3, 5, allow_nan=False), min_size=0, max_size=80),
    future=st.lists(st.floats(-50, 50, allow_nan=False), max_size=10),
)
@settings(max_examples=60, deadline=None)
def test_property_future_trades_do_not_change_score_and_weights_bounded(
    rs: list[float], future: list[float]
) -> None:
    base = series(rs)
    extra = [sample(r, -(i + 1)) for i, r in enumerate(future)]
    assert score_of(base + extra) == score_of(base)
    result = score_of(base)
    assert result.eligible == (
        len(rs) >= CFG.min_trades and result.effective_trades >= CFG.min_effective_trades
    )
    assert 0 <= result.effective_trades <= len(rs) + 1e-9


@pytest.mark.parametrize("min_score", [0.0, -1.0])
def test_combining_strategies_requires_a_positive_min_score(min_score: float) -> None:
    with pytest.raises(ValueError, match="min_score"):
        SelectorConfig(min_score=min_score, max_combined=2)
    assert SelectorConfig(min_score=min_score, max_combined=1).min_score == min_score
