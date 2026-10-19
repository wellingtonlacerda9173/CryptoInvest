from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from engine.application.strategies import (
    ElephantBarBreakout,
    ElephantBarBreakoutConfig,
    EmaCrossTrend,
    EmaCrossTrendConfig,
    LevelsConfig,
    StopMode,
    Strategy,
    StrategyContext,
    StrategySignal,
    candles_are_continuous,
)
from engine.application.strategies.confluence import TRIGGERS as TRIGGER_NAMES
from engine.application.strategies.confluence import independent_families
from engine.domain.candle import Candle
from engine.domain.signal import Signal
from engine.domain.types import Timeframe
from tests.unit.application.helpers import (
    T0,
    Row,
    build_candles,
    candle_row,
    reversal_rows,
    wave_rows,
)

CTX = StrategyContext()
CROSS_PREFIX = 53


def breakout_series(last: Row | None = None) -> list[Candle]:
    rows, _ = wave_rows(70)
    rows.append(last or candle_row(100.0, 102.5, 300.0, 0.1))
    return build_candles(rows)


def assert_valid(result: StrategySignal | None) -> StrategySignal:
    assert result is not None
    sig = result.signal
    assert sig.stop_loss < sig.entry < sig.take_profit
    assert sig.reward_risk >= 2
    assert len(independent_families(result.confluences)) >= 3
    assert not set(result.confluences) & TRIGGER_NAMES
    assert result.triggers
    assert 0 < result.confidence <= 1
    assert result.rationale
    return result


def remove_candle(candles: list[Candle], index: int) -> list[Candle]:
    return [c for i, c in enumerate(candles) if i != index]


def future_candles(after: Candle, count: int = 3) -> list[Candle]:
    return build_candles([candle_row(500.0, 900.0, 9999.0)] * count, start=after.close_time)


class TestContinuity:
    def test_contiguous_series(self) -> None:
        assert candles_are_continuous(breakout_series(), Timeframe.M15)

    def test_empty_and_single_are_continuous(self) -> None:
        assert candles_are_continuous([], Timeframe.M15)
        assert candles_are_continuous(breakout_series()[:1], Timeframe.M15)

    def test_gap_detected(self) -> None:
        assert not candles_are_continuous(remove_candle(breakout_series(), 30), Timeframe.M15)

    def test_old_gap_ignored_when_window_is_recent(self) -> None:
        gapped = remove_candle(breakout_series(), 5)
        assert not candles_are_continuous(gapped, Timeframe.M15)
        assert candles_are_continuous(gapped, Timeframe.M15, last=30)

    def test_wrong_timeframe_rejected(self) -> None:
        assert not candles_are_continuous(breakout_series(), Timeframe.H1)

    def test_duplicate_or_unordered_rejected(self) -> None:
        candles = breakout_series()
        assert not candles_are_continuous([candles[1], candles[0]], Timeframe.M15)
        assert not candles_are_continuous([candles[0], candles[0]], Timeframe.M15)

    def test_mixed_symbols_rejected(self) -> None:
        a = build_candles([candle_row(1, 2)], symbol="BTC/USDT")
        b = build_candles([candle_row(1, 2)], symbol="ETH/USDT", start=T0 + timedelta(minutes=15))
        assert not candles_are_continuous([*a, *b], Timeframe.M15)


class TestElephantBarBreakout:
    def test_protocol_conformance(self) -> None:
        assert isinstance(ElephantBarBreakout(), Strategy)
        assert isinstance(EmaCrossTrend(), Strategy)

    def test_positive_scenario(self) -> None:
        candles = breakout_series()
        result = assert_valid(ElephantBarBreakout().evaluate(candles, CTX))
        sig = result.signal
        assert sig.strategy == "elephant_bar_breakout"
        assert sig.entry == candles[-1].close
        assert sig.created_at == candles[-1].close_time
        assert sig.stop_loss < candles[-1].low
        assert result.triggers == ("barra_elefante_alta", "rompimento_de_resistencia")

    def test_no_volume_no_signal(self) -> None:
        candles = breakout_series(candle_row(100.0, 102.5, 100.0, 0.1))
        assert ElephantBarBreakout().evaluate(candles, CTX) is None

    def test_no_breakout_no_signal(self) -> None:
        # corpo de elefante, mas fecha abaixo da resistencia (~101,6)
        candles = breakout_series(candle_row(98.5, 101.4, 300.0, 0.1))
        assert ElephantBarBreakout().evaluate(candles, CTX) is None

    def test_bearish_elephant_no_signal(self) -> None:
        candles = breakout_series(candle_row(100.0, 97.5, 300.0, 0.1))
        assert ElephantBarBreakout().evaluate(candles, CTX) is None

    def test_gap_in_history_no_signal(self) -> None:
        assert ElephantBarBreakout().evaluate(remove_candle(breakout_series(), 40), CTX) is None

    def test_open_candle_no_signal(self) -> None:
        candles = breakout_series()
        candles[-1] = candles[-1].model_copy(update={"closed": False})
        assert ElephantBarBreakout().evaluate(candles, CTX) is None

    def test_short_history_no_signal(self) -> None:
        assert ElephantBarBreakout().evaluate(breakout_series()[-20:], CTX) is None

    def test_wide_structure_stop_is_rejected(self) -> None:
        config = ElephantBarBreakoutConfig(levels=LevelsConfig(max_risk_atr=1.0))
        assert ElephantBarBreakout(config).evaluate(breakout_series(), CTX) is None

    def test_atr_stop_and_custom_reward_risk(self) -> None:
        levels = LevelsConfig(stop_mode=StopMode.ATR, atr_stop_multiple=2.0, reward_risk=Decimal(3))
        strategy = ElephantBarBreakout(ElephantBarBreakoutConfig(levels=levels))
        result = assert_valid(strategy.evaluate(breakout_series(), CTX))
        assert result.signal.reward_risk >= 3

    def test_higher_timeframe_confluence_counts(self) -> None:
        candles = breakout_series()
        strategy = ElephantBarBreakout()
        up = assert_valid(strategy.evaluate(candles, StrategyContext(htf_trend="up")))
        down = assert_valid(strategy.evaluate(candles, StrategyContext(htf_trend="down")))
        assert "tendencia_superior_de_alta" in up.confluences
        assert "tendencia_superior_de_alta" not in down.confluences
        assert down.confidence < up.confidence

    def test_missing_htf_is_never_more_lenient_than_htf_down(self) -> None:
        candles = breakout_series()
        strategy = ElephantBarBreakout()

        unknown = assert_valid(strategy.evaluate(candles, StrategyContext(htf_trend=None)))
        down = assert_valid(strategy.evaluate(candles, StrategyContext(htf_trend="down")))

        assert unknown.confidence == down.confidence
        assert unknown.confluences == down.confluences
        assert "tendencia_superior_de_alta" not in unknown.confluences

    def test_triggers_do_not_count_as_confluences(self) -> None:
        candles = breakout_series()

        result = assert_valid(ElephantBarBreakout().evaluate(candles, CTX))

        assert not set(result.confluences) & {"barra_elefante_alta", "rompimento_de_resistencia"}

    def test_only_independent_families_count_toward_the_minimum(self) -> None:
        # quatro familias possiveis (volume, tendencia, momentum, estrutura): 5 e inalcancavel
        four = ElephantBarBreakoutConfig(min_confluences=4)
        candles = breakout_series()

        result = ElephantBarBreakout(four).evaluate(candles, StrategyContext(htf_trend="up"))

        assert result is not None
        assert len(independent_families(result.confluences)) == 4
        assert (
            ElephantBarBreakout(ElephantBarBreakoutConfig(min_confluences=5)).evaluate(candles, CTX) is None
        )

    def test_confluence_threshold_is_editable(self) -> None:
        strict = ElephantBarBreakoutConfig(min_confluences=9)
        assert (
            ElephantBarBreakout(strict).evaluate(breakout_series(), StrategyContext(htf_trend="up")) is None
        )

    def test_min_confidence_blocks_signal(self) -> None:
        strict = ElephantBarBreakoutConfig(min_confidence=1.0)
        assert (
            ElephantBarBreakout(strict).evaluate(breakout_series(), StrategyContext(htf_trend="down")) is None
        )

    def test_future_candles_do_not_change_past_decision(self) -> None:
        candles = breakout_series()
        extended = [*candles, *future_candles(candles[-1])]
        strategy = ElephantBarBreakout()
        for cut in range(60, len(candles) + 1):
            assert strategy.evaluate(candles[:cut], CTX) == strategy.evaluate(extended[:cut], CTX)


class TestEmaCrossTrend:
    def signals(
        self, config: EmaCrossTrendConfig | None = None, ctx: StrategyContext = CTX
    ) -> dict[int, StrategySignal]:
        candles = build_candles(reversal_rows())
        strategy = EmaCrossTrend(config)
        found: dict[int, StrategySignal] = {}
        for cut in range(40, len(candles) + 1):
            if (result := strategy.evaluate(candles[:cut], ctx)) is not None:
                found[cut] = result
        return found

    def test_signals_only_on_the_cross_candle(self) -> None:
        found = self.signals()
        assert list(found) == [CROSS_PREFIX]
        result = assert_valid(found[CROSS_PREFIX])
        assert result.signal.strategy == "ema_cross_trend"
        assert result.triggers == ("cruzamento_ema_para_cima", "adx_em_tendencia")

    def test_adx_filter_blocks_signal(self) -> None:
        assert self.signals(EmaCrossTrendConfig(adx_min=99.9)) == {}

    def test_no_signal_without_cross(self) -> None:
        candles = build_candles(reversal_rows())
        assert EmaCrossTrend().evaluate(candles[: CROSS_PREFIX + 10], CTX) is None
        assert EmaCrossTrend().evaluate(candles[: CROSS_PREFIX - 1], CTX) is None

    def test_downtrend_never_signals(self) -> None:
        rows: list[Row] = []
        price = 100.0
        for _ in range(80):
            rows.append(candle_row(price, price - 0.5, 100.0))
            price -= 0.5
        candles = build_candles(rows)
        assert all(EmaCrossTrend().evaluate(candles[:n], CTX) is None for n in range(52, 81))

    def test_gap_blocks_signal(self) -> None:
        candles = remove_candle(build_candles(reversal_rows()), 30)
        assert EmaCrossTrend().evaluate(candles[: CROSS_PREFIX - 1], CTX) is None

    def test_missing_htf_is_never_more_lenient_than_htf_down(self) -> None:
        permissive = EmaCrossTrendConfig(min_confidence=0.0)

        unknown = self.signals(permissive, StrategyContext(htf_trend=None))[CROSS_PREFIX]
        down = self.signals(permissive, StrategyContext(htf_trend="down"))[CROSS_PREFIX]

        assert unknown.confidence == down.confidence
        assert unknown.confluences == down.confluences

    def test_htf_down_reduces_confidence(self) -> None:
        up = self.signals(ctx=StrategyContext(htf_trend="up"))[CROSS_PREFIX]
        permissive = EmaCrossTrendConfig(min_confidence=0.0)
        down = self.signals(permissive, StrategyContext(htf_trend="down"))[CROSS_PREFIX]
        assert up.confidence > down.confidence

    def test_structure_stop_mode_is_supported(self) -> None:
        config = EmaCrossTrendConfig(levels=LevelsConfig(stop_mode=StopMode.STRUCTURE, max_risk_atr=50.0))
        assert_valid(self.signals(config)[CROSS_PREFIX])

    def test_future_candles_do_not_change_past_decision(self) -> None:
        candles = build_candles(reversal_rows())
        extended = [*candles[:CROSS_PREFIX], *future_candles(candles[CROSS_PREFIX - 1], 5)]
        strategy = EmaCrossTrend()
        assert strategy.evaluate(candles[:CROSS_PREFIX], CTX) == strategy.evaluate(
            extended[:CROSS_PREFIX], CTX
        )
        assert strategy.evaluate(candles[:CROSS_PREFIX], CTX) is not None

    @pytest.mark.parametrize(
        "overrides",
        [
            {"fast_period": 21, "slow_period": 9},
            {"min_confluences": 2},
            {"rsi_range": (70.0, 50.0)},
            {"adx_min": 0},
        ],
    )
    def test_invalid_config_rejected(self, overrides: dict[str, object]) -> None:
        with pytest.raises(ValidationError):
            EmaCrossTrendConfig(**overrides)  # type: ignore[arg-type]


class TestModels:
    def test_levels_reject_reward_risk_below_two(self) -> None:
        with pytest.raises(ValidationError):
            LevelsConfig(reward_risk=Decimal("1.5"))

    def test_levels_reject_float_reward_risk(self) -> None:
        with pytest.raises(ValidationError):
            LevelsConfig(reward_risk=3.0)  # type: ignore[arg-type]

    def test_strategy_signal_requires_three_independent_families(self) -> None:
        signal = Signal(
            symbol="BTC/USDT",
            timeframe=Timeframe.M15,
            created_at=T0,
            strategy="x",
            entry=Decimal(100),
            stop_loss=Decimal(99),
            take_profit=Decimal(102),
        )
        correlated = ("dominio_comprador_di", "preco_acima_da_ema_de_tendencia", "tendencia_superior_de_alta")
        with_triggers = ("barra_elefante_alta", "rompimento_de_resistencia", "volume_forte")
        independent = ("volume_forte", "dominio_comprador_di", "rsi_com_espaco")
        for confluences in (correlated, with_triggers, ("volume_forte", "volume_forte", "rsi_com_espaco")):
            with pytest.raises(ValidationError, match="families"):
                StrategySignal(signal=signal, confidence=0.5, rationale="r", confluences=confluences)
        assert StrategySignal(signal=signal, confidence=0.5, rationale="r", confluences=independent)

    def test_breakout_config_defaults_follow_spec(self) -> None:
        config = ElephantBarBreakoutConfig()
        assert config.elephant.lookback == 20
        assert config.elephant.body_avg_multiple == 1.8
        assert config.elephant.min_body_ratio == 0.7
        assert config.elephant.volume_multiple == 1.5
        assert config.min_confluences == 3
