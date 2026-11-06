import math
from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from engine.application.indicators import CandleArrays, atr, ema
from engine.application.risk.sizing import net_reward_risk_ok
from engine.application.strategies import (
    STRATEGY_REGISTRY,
    LevelsConfig,
    PullbackTrend,
    PullbackTrendConfig,
    Strategy,
    StrategyContext,
    StrategySignal,
)
from engine.application.strategies.confluence import TRIGGERS, independent_families
from engine.application.strategies.pullback_trend import is_uptrend
from engine.application.strategies.registry import EXPERIMENTAL_STRATEGIES, default_strategy_names
from engine.application.trading_config.document import StrategiesConfig
from engine.domain.candle import Candle
from engine.domain.risk import RiskConfig
from engine.domain.types import Timeframe
from engine.runner.paper_config import PaperRunConfig
from tests.unit.application.helpers import Row, build_candles, candle_row

FEE = RiskConfig().taker_fee_rate
CTX = StrategyContext(taker_fee_rate=FEE)
RISING = 250
NEEDED = 200 + 10 + 1


def rising_rows(count: int = RISING) -> tuple[list[Row], float]:
    rows: list[Row] = []
    price = 100.0
    for i in range(count):
        nxt = price + 0.5 + 0.1 * math.sin(i)
        rows.append(candle_row(price, nxt, wick=0.2))
        price = nxt
    return rows, price


def dip_rows(depth: int = 6, step: float = 1.0) -> tuple[list[Row], float]:
    rows, price = rising_rows()
    for _ in range(depth):
        rows.append(candle_row(price, price - step, wick=0.2))
        price -= step
    return rows, price


def candles_of(rows: list[Row]) -> list[Candle]:
    return build_candles(rows, Timeframe.H1)


def trigger_candles(rise: float = 2.5) -> list[Candle]:
    rows, price = dip_rows()
    return candles_of([*rows, candle_row(price, price + rise, wick=0.2)])


def evaluate(candles: list[Candle], **config: object) -> StrategySignal | None:
    return PullbackTrend(PullbackTrendConfig.model_validate(config)).evaluate(candles, CTX)


def signal_of(candles: list[Candle], **config: object) -> StrategySignal:
    result = evaluate(candles, **config)
    assert result is not None
    return result


def indicators(candles: list[Candle]) -> tuple[CandleArrays, float]:
    data = CandleArrays.from_candles(candles)
    return data, float(atr(data.high, data.low, data.close, 14)[-1])


class TestTrigger:
    def test_signal_on_pullback_resumption(self) -> None:
        candles = trigger_candles()
        strategy = PullbackTrend()
        assert isinstance(strategy, Strategy)
        result = signal_of(candles)
        sig = result.signal
        assert sig.strategy == strategy.name == "pullback_trend"
        assert sig.entry == candles[-1].close
        assert sig.created_at == candles[-1].close_time
        assert result.triggers
        assert set(result.triggers) <= TRIGGERS
        assert len(independent_families(result.confluences)) >= 3
        assert result.confidence == 1.0

    def test_scenario_really_satisfies_each_rule(self) -> None:
        data, _ = indicators(trigger_candles())
        fast, trend = ema(data.close, 20), ema(data.close, 200)
        assert is_uptrend(data.close[-1], fast[-1], trend[-1], trend[-11])
        assert (data.low[-5:] <= fast[-5:]).any()
        assert data.close[-1] > fast[-1]
        assert data.close[-1] > data.high[-2]

    def test_no_signal_before_the_trigger_candle(self) -> None:
        assert evaluate(trigger_candles()[:-1]) is None

    def test_no_pullback_means_no_signal(self) -> None:
        rows, price = rising_rows()
        candles = candles_of([*rows, candle_row(price, price + 2.5, wick=0.2)])
        data, _ = indicators(candles)
        fast = ema(data.close, 20)
        assert not (data.low[-5:] <= fast[-5:]).any()
        assert data.close[-1] > data.high[-2]
        assert data.close[-1] > fast[-1]
        assert evaluate(candles) is None

    def test_pullback_older_than_the_window_does_not_count(self) -> None:
        rows, price = dip_rows()
        for _ in range(7):
            rows.append(candle_row(price, price + 1.5, wick=0.2))
            price += 1.5
        late = candles_of([*rows, candle_row(price, price + 2.5, wick=0.2)])
        data, _ = indicators(late)
        fast = ema(data.close, 20)
        assert not (data.low[-5:] <= fast[-5:]).any()
        assert data.close[-1] > data.high[-2]
        assert evaluate(late, pullback_bars=5) is None
        wide = LevelsConfig(stop_buffer_atr=0.0, max_risk_atr=40.0)
        assert evaluate(late, pullback_bars=9, levels=wide) is not None

    def test_close_not_above_previous_high_means_no_signal(self) -> None:
        rows, price = dip_rows()
        rows.append(candle_row(price, price + 2.5, wick=0.2))
        price += 2.5
        candles = candles_of([*rows, candle_row(price, price + 0.1, wick=0.05)])
        data, _ = indicators(candles)
        assert data.close[-1] > ema(data.close, 20)[-1]
        assert data.close[-1] <= data.high[-2]
        assert evaluate(candles) is None

    def test_close_not_above_fast_ema_means_no_signal(self) -> None:
        rows, price = dip_rows()
        candles = candles_of([*rows, candle_row(price, price + 1.3, wick=0.2)])
        data, _ = indicators(candles)
        assert data.close[-1] > data.high[-2]
        assert data.close[-1] <= ema(data.close, 20)[-1]
        assert evaluate(candles) is None

    def test_downtrend_means_no_signal(self) -> None:
        rows: list[Row] = []
        price = 300.0
        for i in range(RISING):
            nxt = price - 0.5 + 0.1 * math.sin(i)
            rows.append(candle_row(price, nxt, wick=0.2))
            price = nxt
        rows.append(candle_row(price, price + 3.0, wick=0.2))
        assert evaluate(candles_of(rows)) is None


class TestTrendRule:
    def test_all_three_parts_hold(self) -> None:
        assert is_uptrend(110.0, 105.0, 100.0, 99.0)

    @pytest.mark.parametrize(
        ("close", "fast", "trend", "before"),
        [
            pytest.param(100.0, 105.0, 100.0, 99.0, id="close_not_above_trend"),
            pytest.param(110.0, 100.0, 100.0, 99.0, id="fast_not_above_trend"),
            pytest.param(110.0, 105.0, 100.0, 100.0, id="trend_flat"),
            pytest.param(110.0, 105.0, 100.0, 101.0, id="trend_falling"),
        ],
    )
    def test_each_part_failing_alone_blocks(
        self, close: float, fast: float, trend: float, before: float
    ) -> None:
        assert not is_uptrend(close, fast, trend, before)

    def test_slope_is_read_slope_bars_candles_back(self) -> None:
        candles = trigger_candles()
        data, _ = indicators(candles)
        trend = ema(data.close, 200)
        assert trend[-1] > trend[-1 - 10]
        assert signal_of(candles, slope_bars=10)


class TestGuards:
    def test_insufficient_history(self) -> None:
        candles = trigger_candles()
        assert evaluate(candles[-NEEDED:]) is not None
        assert evaluate(candles[-(NEEDED - 1) :]) is None
        assert evaluate([]) is None

    def test_open_last_candle(self) -> None:
        candles = trigger_candles()
        candles[-1] = candles[-1].model_copy(update={"closed": False})
        assert evaluate(candles) is None

    def test_open_candle_in_history(self) -> None:
        candles = trigger_candles()
        candles[-30] = candles[-30].model_copy(update={"closed": False})
        assert evaluate(candles) is None

    def test_gap_in_recent_history(self) -> None:
        candles = trigger_candles()
        assert evaluate([c for i, c in enumerate(candles) if i != len(candles) - 10]) is None

    def test_foreign_symbol_in_history(self) -> None:
        candles = trigger_candles()
        candles[-20] = candles[-20].model_copy(update={"symbol": "ETH/USDT"})
        assert evaluate(candles) is None

    def test_future_candles_never_change_a_past_decision(self) -> None:
        candles = trigger_candles()
        wild = build_candles([candle_row(1.0, 900.0, 9999.0)] * 5, Timeframe.H1, start=candles[-1].close_time)
        extended = [*candles, *wild]
        strategy = PullbackTrend()
        for cut in range(len(candles) - 8, len(candles) + 1):
            assert strategy.evaluate(candles[:cut], CTX) == strategy.evaluate(extended[:cut], CTX)

    def test_decision_does_not_depend_on_the_calendar_date(self) -> None:
        candles = trigger_candles()
        shifted = [c.model_copy(update={"open_time": c.open_time + timedelta(days=400)}) for c in candles]
        assert signal_of(shifted).signal.entry == signal_of(candles).signal.entry


class TestLevels:
    @pytest.mark.parametrize(
        ("depth", "step", "rise"), [(4, 1.0, 2.5), (5, 1.0, 2.0), (6, 1.0, 3.5), (5, 1.0, 3.0)]
    )
    def test_stop_always_below_entry_and_target_above(self, depth: int, step: float, rise: float) -> None:
        rows, price = dip_rows(depth, step)
        candles = candles_of([*rows, candle_row(price, price + rise, wick=0.2)])
        sig = signal_of(candles).signal
        assert 0 < sig.stop_loss < sig.entry < sig.take_profit

    def test_stop_is_the_swing_low_when_farther_than_the_min_distance(self) -> None:
        candles = trigger_candles()
        _, atr_value = indicators(candles)
        sig = signal_of(candles).signal
        swing = min(c.low for c in candles[-10:])
        assert swing < sig.entry - Decimal(str(0.5 * atr_value))
        assert abs(sig.stop_loss - swing) < Decimal("0.00000002")

    def test_stop_respects_the_min_distance_when_the_swing_is_too_close(self) -> None:
        candles = trigger_candles()
        _, atr_value = indicators(candles)
        levels = LevelsConfig(stop_buffer_atr=0.0, min_risk_atr=20.0, max_risk_atr=40.0)
        sig = signal_of(candles, levels=levels).signal
        assert float(sig.entry - sig.stop_loss) == pytest.approx(20.0 * atr_value, rel=1e-6)
        assert sig.stop_loss < min(c.low for c in candles[-10:])

    def test_default_min_distance_is_half_an_atr(self) -> None:
        candles = trigger_candles()
        _, atr_value = indicators(candles)
        sig = signal_of(candles).signal
        assert float(sig.entry - sig.stop_loss) >= 0.5 * atr_value

    def test_target_is_two_r_net_of_fees(self) -> None:
        sig = signal_of(trigger_candles()).signal
        assert sig.reward_risk >= 2
        assert net_reward_risk_ok(sig.entry, sig.stop_loss, sig.take_profit, FEE, Decimal(2))
        assert not net_reward_risk_ok(
            sig.entry, sig.stop_loss, sig.take_profit - Decimal("0.0001"), FEE, Decimal(2)
        )

    def test_reward_risk_is_configurable_and_never_below_two(self) -> None:
        candles = trigger_candles()
        low = signal_of(candles).signal
        high = signal_of(candles, levels=LevelsConfig(stop_buffer_atr=0.0, reward_risk=Decimal(3))).signal
        assert high.take_profit > low.take_profit
        assert net_reward_risk_ok(high.entry, high.stop_loss, high.take_profit, FEE, Decimal(3))
        with pytest.raises(ValidationError):
            LevelsConfig(reward_risk=Decimal("1.5"))

    def test_taker_fee_in_context_moves_the_target(self) -> None:
        candles = trigger_candles()
        cheap = PullbackTrend().evaluate(candles, StrategyContext(taker_fee_rate=Decimal(0)))
        costly = PullbackTrend().evaluate(candles, StrategyContext(taker_fee_rate=Decimal("0.005")))
        assert cheap is not None
        assert costly is not None
        assert costly.signal.take_profit > cheap.signal.take_profit

    def test_stop_wider_than_the_project_ceiling_is_dropped(self) -> None:
        levels = LevelsConfig(stop_buffer_atr=0.0, max_risk_atr=1.0)
        assert evaluate(trigger_candles(), levels=levels) is None


class TestConfigAndCatalog:
    def test_defaults_match_the_script(self) -> None:
        cfg = PullbackTrendConfig()
        assert (cfg.trend_period, cfg.pullback_period, cfg.pullback_bars) == (200, 20, 5)
        assert (cfg.slope_bars, cfg.swing_bars, cfg.atr_period) == (10, 10, 14)
        assert cfg.levels.min_risk_atr == 0.5
        assert cfg.levels.stop_buffer_atr == 0.0
        assert cfg.levels.reward_risk == Decimal(2)

    def test_config_is_frozen_and_validated(self) -> None:
        cfg = PullbackTrendConfig()
        with pytest.raises(ValidationError):
            cfg.pullback_bars = 3  # type: ignore[misc]
        with pytest.raises(ValidationError):
            PullbackTrendConfig(pullback_period=200, trend_period=200)
        with pytest.raises(ValidationError):
            PullbackTrendConfig(min_confluences=2)

    def test_registered_but_experimental_and_off_by_default(self) -> None:
        assert STRATEGY_REGISTRY["pullback_trend"]().name == "pullback_trend"
        assert "pullback_trend" in EXPERIMENTAL_STRATEGIES
        assert "pullback_trend" not in default_strategy_names()

    def test_not_enabled_in_paper_or_versioned_defaults(self) -> None:
        assert "pullback_trend" not in StrategiesConfig().names
        assert "pullback_trend" not in PaperRunConfig.model_fields["strategies"].default
