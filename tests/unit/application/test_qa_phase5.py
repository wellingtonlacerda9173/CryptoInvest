"""QA da Fase 5: vazamento de futuro com estrategias reais, formula do score e poder estatistico."""

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import numpy as np
import pytest

from engine.application.backtest import BacktestEngine, BacktestResult, BacktestSettings, DecisionAction
from engine.application.regime import Regime
from engine.application.selector import SelectorConfig, SelectorSettings, TradeSample, compute_score
from engine.application.strategies import Strategy
from engine.application.strategies.registry import STRATEGY_REGISTRY
from engine.application.validation import corrected_alpha
from engine.domain.candle import Candle
from engine.domain.types import Timeframe
from engine.runner import validate

START = datetime(2024, 1, 1, tzinfo=UTC)
NOW = datetime(2030, 1, 1, tzinfo=UTC)
NAMES = ("donchian_breakout", "vwap_reclaim", "supertrend_flip", "bullish_engulfing")
CUT = 1000
OPERABLE = (Regime.TENDENCIA_ALTA, Regime.TENDENCIA_BAIXA, Regime.LATERAL)


def walk(count: int, seed: int) -> list[Candle]:
    """Passeio aleatorio com volatilidade em agrupamentos, deriva lenta e volume ligado ao movimento."""
    rng = np.random.default_rng(seed)
    step = timedelta(seconds=Timeframe.H1.seconds)
    price, vol, out = 100.0, 0.004, []
    for i in range(count):
        vol = 0.9 * vol + 0.1 * 0.004 * (1 + 2 * abs(rng.standard_normal()))
        ret = 0.0006 * math.sin(i / 200) + vol * rng.standard_normal()
        open_, close = price, price * (1 + ret)
        high = max(open_, close) * (1 + abs(rng.standard_normal()) * vol * 0.5)
        low = min(open_, close) * (1 - abs(rng.standard_normal()) * vol * 0.5)
        volume = 1000 * (1 + 0.5 * abs(rng.standard_normal())) * (1 + 8 * abs(ret) / 0.004 * 0.1)
        out.append(
            Candle(
                symbol="BTC/USDT",
                timeframe=Timeframe.H1,
                open_time=START + step * i,
                open=Decimal(f"{open_:.6f}"),
                high=Decimal(f"{high:.6f}"),
                low=Decimal(f"{low:.6f}"),
                close=Decimal(f"{close:.6f}"),
                volume=Decimal(f"{volume:.4f}"),
                closed=True,
            )
        )
        price = close
    return out


def shaken(candles: list[Candle], cut: int) -> list[Candle]:
    tail = [
        c.model_copy(
            update={
                "open": c.open * 2,
                "high": c.high * 3,
                "low": c.low / 2,
                "close": c.close * Decimal("2.5"),
            }
        )
        for c in candles[cut:]
    ]
    return [*candles[:cut], *tail]


def history(strategy: str, mean_r: float) -> list[TradeSample]:
    out: list[TradeSample] = []
    for regime in OPERABLE:
        for k in range(40):
            end = START + timedelta(hours=200) - timedelta(hours=k)
            out.append(
                TradeSample(
                    strategy=strategy,
                    symbol="BTC/USDT",
                    regime=regime,
                    opened_at=end - timedelta(hours=2),
                    closed_at=end,
                    r_multiple=mean_r + (0.1 if k % 2 else -0.1),
                )
            )
    return out


def strategies() -> list[Strategy]:
    return [STRATEGY_REGISTRY[n]() for n in NAMES]


async def run(candles: list[Candle], selector: SelectorSettings | None = None) -> BacktestResult:
    samples = [s for n in NAMES for s in history(n, 2.0)]
    engine = BacktestEngine(
        strategies(), BacktestSettings(), clock=lambda: NOW, selector=selector, selector_history=samples
    )
    return await engine.run(candles)


def known_before(
    result: BacktestResult, cutoff: datetime
) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    decisions = [
        (d.at, d.strategy, d.action, d.code, d.entry, d.stop, d.target, repr(d.context))
        for d in result.decisions
        if d.at < cutoff
    ]
    trips = [
        (t.opened_at, t.strategy, t.entry_price, t.quantity)
        for t in result.round_trips
        if t.opened_at < cutoff
    ]
    return decisions, trips


def selector_on() -> SelectorSettings:
    cfg = SelectorConfig(
        min_trades=3, min_effective_trades=2.0, min_score=0.001, max_combined=4, combine_margin=100.0
    )
    return SelectorSettings(enabled=True, selector=cfg, recalc_hours=1)


@pytest.mark.parametrize("use_selector", [False, True])
async def test_real_strategies_do_not_see_future_candles_through_the_engine(use_selector: bool) -> None:
    # Given uma serie e a mesma serie com todas as velas apos CUT adulteradas
    base = walk(1600, seed=11)
    cutoff = base[CUT].open_time
    selector = selector_on() if use_selector else None

    # When roda o motor completo (estrategias novas, regime, seletor, gate de risco) nas duas
    reference = await run(base, selector)
    changed = await run(shaken(base, CUT), selector)

    # Then tudo o que foi decidido/aberto antes de CUT e identico
    expected = known_before(reference, cutoff)
    assert sum(1 for d in expected[0] if d[2] is DecisionAction.TRADED) >= 3, "serie sem sinais: teste vazio"
    assert known_before(changed, cutoff) == expected


async def test_every_order_has_a_stop_below_entry_and_target_at_least_two_r() -> None:
    result = await run(walk(1600, seed=11))
    traded = [d for d in result.decisions if d.action is DecisionAction.TRADED]
    assert len(traded) >= 3
    for d in traded:
        assert d.entry is not None and d.stop is not None and d.target is not None
        assert 0 < d.stop < d.entry < d.target
        assert (d.target - d.entry) / (d.entry - d.stop) >= Decimal(2)


def test_score_matches_hand_computation() -> None:
    # 20 ganhos (+1) e depois 10 perdas (-1); sem decaimento relevante (trades a 1 s uns dos outros)
    as_of = datetime(2026, 6, 1, tzinfo=UTC)
    samples = []
    for k in range(30):
        closed = as_of - timedelta(seconds=30 - k)
        samples.append(
            TradeSample(
                strategy="A",
                symbol="BTC/USDT",
                regime=Regime.TENDENCIA_ALTA,
                opened_at=closed - timedelta(seconds=1),
                closed_at=closed,
                r_multiple=1.0 if k < 20 else -1.0,
            )
        )
    score = compute_score("A", Regime.TENDENCIA_ALTA, "BTC/USDT", samples, as_of, SelectorConfig())
    variance = (8 / 9) * 30 / 29
    lower = 1 / 3 - 1.645 * math.sqrt(variance / 30)
    instability = float(np.std([1.0, 1.0, -1.0]))
    expected = lower - 0.5 * 10 / 30 - 0.5 * instability
    assert score.mean_r == pytest.approx(1 / 3, abs=1e-4)
    assert score.lower_bound_r == pytest.approx(lower, abs=1e-3)
    assert score.score == pytest.approx(expected, abs=1e-3)
    assert score.effective_trades == pytest.approx(30, abs=1e-3)


def test_corrected_alpha_for_the_reported_family() -> None:
    assert corrected_alpha(0.05, 78, "sidak") == pytest.approx(0.000657, abs=1e-6)


def test_BUG_default_simulations_can_reach_the_corrected_alpha_of_the_reported_family() -> None:
    args = validate.build_parser().parse_args(["--end", "2026-10-01T00:00:00Z"])
    m = validate.count_comparisons(list(validate.STRATEGY_REGISTRY), ["A", "B", "C"], ["1h", "4h"])
    alpha_c = corrected_alpha(args.alpha, m, args.correction)
    assert validate.resolve_simulations(args, m) >= 10_000
    assert 1 / (validate.resolve_simulations(args, m) + 1) < alpha_c
