import itertools
import random
from collections.abc import Mapping, Sequence
from datetime import timedelta
from decimal import Decimal

import pytest

from engine.application.backtest import ParameterGrid, WalkForwardConfig, metrics_from_parts, walk_forward
from engine.application.backtest.walk_forward import (
    Candidate,
    WindowResult,
    _assess,
    _selection_key,
    _window_bounds,
)
from engine.application.strategies import Strategy, StrategyContext, StrategySignal
from engine.domain.candle import Candle
from engine.domain.signal import Signal
from tests.unit.backtest.engine_helpers import CONFLUENCES, T0, D, mk, settings
from tests.unit.backtest.test_metrics import curve, trip

CONFIG = WalkForwardConfig(train_days=7, test_days=3, min_train_trades=2, require_positive_train=False)
GRID = ParameterGrid(values={"rr": (D("2.5"), D("3.5"))})
QUANT = Decimal("0.01")


class Periodic:
    """Um sinal por dia (04:00 UTC) com alvo = rr x risco; deterministico e dependente so das velas."""

    name = "periodic"

    def __init__(self, rr: Decimal) -> None:
        self.rr = rr

    def evaluate(self, candles: Sequence[Candle], context: StrategyContext) -> StrategySignal | None:
        last = candles[-1]
        if last.open_time.hour != 4:
            return None
        entry = last.close
        stop = (entry * D("0.98")).quantize(QUANT)
        target = (entry + (entry - stop) * self.rr).quantize(QUANT)
        signal = Signal(
            symbol=last.symbol,
            timeframe=last.timeframe,
            created_at=last.close_time,
            strategy=self.name,
            entry=entry,
            stop_loss=stop,
            take_profit=target,
        )
        return StrategySignal(
            signal=signal, confidence=0.7, rationale="t", triggers=(), confluences=CONFLUENCES
        )


def factory(params: Mapping[str, Decimal]) -> list[Strategy]:
    return [Periodic(params["rr"])]


def random_walk(days: int, seed: int, from_index: int = 0, base: list[Candle] | None = None) -> list[Candle]:
    rng = random.Random(seed)  # noqa: S311 - dados sinteticos de teste
    candles = list(base or [])
    price = candles[-1].close if candles else D(100)
    for i in range(len(candles), days * 24):
        move = D(str(round(rng.gauss(0.0004, 0.004), 5)))
        close = (price * (1 + move)).quantize(QUANT)
        high = max(price, close) * D("1.003")
        low = min(price, close) * D("0.997")
        candles.append(mk(i, str(price), str(high.quantize(QUANT)), str(low.quantize(QUANT)), str(close)))
        price = close
    return candles


def test_window_bounds_are_sequential_rolling_and_never_overlap_train_with_test() -> None:
    bounds = _window_bounds(19 * 24, 24, CONFIG)

    assert bounds == [(0, 168, 240), (72, 240, 312), (144, 312, 384), (216, 384, 456)]
    assert all(nxt[1] == prev[2] for prev, nxt in itertools.pairwise(bounds))
    assert all(b[2] <= 19 * 24 for b in bounds)


def test_step_smaller_than_test_would_overlap_the_out_of_sample_windows() -> None:
    with pytest.raises(ValueError, match="step_days"):
        WalkForwardConfig(train_days=7, test_days=5, step_days=3)


def test_selection_prefers_lower_drawdown_then_consistency_then_return() -> None:
    def cand(dd: float, months: float | None, ret: float) -> Candidate:
        return Candidate(
            params={},
            trades=9,
            max_drawdown=dd,
            positive_months_ratio=months,
            total_return=ret,
            eligible=True,
        )

    ranked = sorted(
        [
            (0, cand(0.03, 0.5, 0.20)),
            (1, cand(0.004, 0.4, 0.01)),
            (2, cand(0.011, 0.9, 0.05)),
            (3, cand(0.014, 0.9, 0.09)),
            (4, cand(0.012, 0.4, 0.50)),
        ],
        key=lambda item: _selection_key(*item),
    )

    assert [i for i, _ in ranked] == [1, 3, 2, 4, 0]


async def test_selection_uses_only_training_data_future_candles_cannot_leak() -> None:
    a = random_walk(19, seed=1)
    first_test_start = 7 * 24
    b = random_walk(19, seed=99, base=a[:first_test_start])
    assert a[:first_test_start] == b[:first_test_start]
    assert a[first_test_start:] != b[first_test_start:]

    ra = await walk_forward(a, factory, GRID, settings(), CONFIG)
    rb = await walk_forward(b, factory, GRID, settings(), CONFIG)

    wa, wb = ra.windows[0], rb.windows[0]
    assert wa.candidates == wb.candidates
    assert wa.selected == wb.selected
    assert wa.train_metrics == wb.train_metrics
    assert wa.test_metrics != wb.test_metrics


async def test_oos_equity_chains_window_returns_and_windows_restart_from_initial_capital() -> None:
    result = await walk_forward(random_walk(19, seed=3), factory, GRID, settings(), CONFIG)

    assert len(result.windows) == 4
    assert all(w.test_start > w.train_end - timedelta(seconds=1) for w in result.windows)
    growth = Decimal(1)
    for window in result.windows:
        assert window.test_result is not None
        assert window.test_result.initial_capital == D(10_000)
        assert window.test_result.period_start == window.test_start
        growth *= 1 + window.test_result.final_equity / D(10_000) - 1
    assert result.out_of_sample.final_equity == pytest.approx(D(10_000) * growth, rel=D("1e-9"))
    assert len(result.out_of_sample_curve) == 4 * 3 * 24
    assert result.out_of_sample.trades == sum(
        len(w.test_result.round_trips) for w in result.windows if w.test_result
    )


async def test_no_eligible_parameters_means_the_robot_stays_idle() -> None:
    config = CONFIG.model_copy(update={"min_train_trades": 10_000})

    result = await walk_forward(random_walk(19, seed=3), factory, GRID, settings(), config)

    assert all(w.selected is None and w.test_result is None for w in result.windows)
    assert result.out_of_sample.trades == 0
    assert result.out_of_sample.idle_pct == 1.0
    assert result.out_of_sample.total_return == 0
    assert result.assessment.verdict.startswith("sem selecao")


async def test_walk_forward_is_deterministic() -> None:
    candles = random_walk(19, seed=5)

    first = await walk_forward(candles, factory, GRID, settings(), CONFIG)
    second = await walk_forward(candles, factory, GRID, settings(), CONFIG)

    assert first.out_of_sample == second.out_of_sample
    assert [w.selected for w in first.windows] == [w.selected for w in second.windows]


def test_overfit_flags_when_the_edge_disappears_out_of_sample() -> None:
    good_train = metrics_from_parts(D(1000), curve([1100]), [trip(100)])
    window = WindowResult(
        index=1,
        train_start=T0,
        train_end=T0,
        test_start=T0,
        test_end=T0,
        candidates=(),
        selected={"rr": D(3)},
        train_metrics=good_train,
        test_metrics=None,
        test_result=None,
    )
    bad_oos = metrics_from_parts(D(1000), curve([900]), [trip(-10, day=i) for i in range(30)])

    assessment = _assess([window, window], bad_oos, CONFIG)

    assert assessment.verdict == "indicios de sobreajuste"
    assert len(assessment.flags) >= 2
    assert assessment.in_sample_expectancy_r == 1.0
    assert assessment.out_of_sample_expectancy_r == pytest.approx(-0.1)


def test_few_out_of_sample_trades_are_inconclusive_not_a_verdict() -> None:
    train = metrics_from_parts(D(1000), curve([1100]), [trip(100)])
    window = WindowResult(
        index=1,
        train_start=T0,
        train_end=T0,
        test_start=T0,
        test_end=T0,
        candidates=(),
        selected={"rr": D(3)},
        train_metrics=train,
        test_metrics=None,
        test_result=None,
    )

    assessment = _assess([window], metrics_from_parts(D(1000), curve([1000]), []), CONFIG)

    assert assessment.verdict.startswith("inconclusivo")
