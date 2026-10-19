from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from engine.application.strategies import ElephantBarBreakout, EmaCrossTrend, StopMode
from engine.domain.types import Timeframe
from engine.runner.backtest import build_parser, build_strategies, parse_grid, parse_values
from engine.runner.candle_source import (
    cache_path,
    floor_to_timeframe,
    longest_contiguous,
    merge_candles,
    missing_ranges,
    read_cache,
    write_cache,
)
from tests.unit.backtest.engine_helpers import T0, mk

D = Decimal
H = timedelta(hours=1)


class TestParsing:
    def test_grid_expands_every_combination(self) -> None:
        grid = parse_grid("min_confidence=0.5,0.6;rsi_min=45,50;reward_risk=2,3")

        combos = grid.combinations()
        assert len(combos) == 8
        assert {"min_confidence": D("0.6"), "reward_risk": D(2), "rsi_min": D(45)} in combos

    @pytest.mark.parametrize("text", ["foo=1,2", "min_confidence=", ""])
    def test_invalid_grid_is_rejected(self, text: str) -> None:
        with pytest.raises(ValueError, match=r"parametros|grade"):
            parse_grid(text)

    def test_fixed_values(self) -> None:
        assert parse_values("min_confidence=0.7, rsi_min=48") == {
            "min_confidence": D("0.7"),
            "rsi_min": D(48),
        }
        assert parse_values("") == {}

    def test_parser_defaults_match_the_documented_example(self) -> None:
        args = build_parser().parse_args(["--symbol", "BTC/USDT"])

        assert (args.timeframe, args.years, args.capital, args.slippage_bps) == ("1h", 2.0, D(10_000), D(5))
        assert (args.taker_fee, args.maker_fee, args.latency, args.walk_forward) == (
            D("0.001"),
            D("0.001"),
            1,
            False,
        )
        assert args.out == Path("reports")


class TestStrategyFactory:
    def test_defaults_keep_each_strategy_configuration(self) -> None:
        elephant, ema = build_strategies(["elephant", "ema"], {})

        assert isinstance(elephant, ElephantBarBreakout)
        assert isinstance(ema, EmaCrossTrend)
        assert (elephant.config.min_confidence, elephant.config.rsi_min) == (0.5, 50.0)
        assert ema.config.rsi_range == (50.0, 70.0)
        assert ema.config.levels.stop_mode is StopMode.ATR

    def test_parameters_reach_both_strategies(self) -> None:
        params = {"min_confidence": D("0.65"), "rsi_min": D(45), "reward_risk": D(3)}
        elephant, ema = build_strategies(["elephant", "ema"], params)

        assert isinstance(elephant, ElephantBarBreakout)
        assert isinstance(ema, EmaCrossTrend)
        assert (elephant.config.min_confidence, elephant.config.rsi_min) == (0.65, 45.0)
        assert elephant.config.levels.reward_risk == D(3)
        assert ema.config.rsi_range == (45.0, 70.0)
        assert ema.config.levels.reward_risk == D(3)
        assert ema.config.levels.stop_mode is StopMode.ATR

    def test_unknown_strategy(self) -> None:
        with pytest.raises(ValueError, match="desconhecida"):
            build_strategies(["nope"], {})


class TestCandleSource:
    def test_floor_to_timeframe(self) -> None:
        moment = datetime(2026, 3, 10, 12, 34, 56, tzinfo=UTC)

        assert floor_to_timeframe(moment, Timeframe.H1) == datetime(2026, 3, 10, 12, tzinfo=UTC)
        assert floor_to_timeframe(moment, Timeframe.D1) == datetime(2026, 3, 10, tzinfo=UTC)

    def test_merge_is_sorted_and_deduplicated_with_the_latest_source_winning(self) -> None:
        old = [mk(2, "1", "2", "1", "1"), mk(1, "1", "2", "1", "1")]
        new = [mk(2, "1", "3", "1", "2"), mk(3, "1", "2", "1", "1")]

        merged = merge_candles(old, new)

        assert [c.open_time for c in merged] == [T0 + i * H for i in (1, 2, 3)]
        assert merged[1].close == D(2)

    def test_missing_ranges_find_head_tail_and_internal_holes(self) -> None:
        candles = [mk(i, "1", "2", "1", "1") for i in (2, 3, 6, 7)]

        gaps = missing_ranges(candles, T0, T0 + 10 * H, Timeframe.H1)

        assert gaps == [(T0, T0 + H), (T0 + 4 * H, T0 + 5 * H), (T0 + 8 * H, T0 + 9 * H)]

    def test_nothing_missing_when_complete(self) -> None:
        candles = [mk(i, "1", "2", "1", "1") for i in range(5)]

        assert missing_ranges(candles, T0, T0 + 5 * H, Timeframe.H1) == []
        assert missing_ranges([], T0, T0 + 5 * H, Timeframe.H1) == [(T0, T0 + 4 * H)]

    def test_longest_contiguous_run_and_hole_count(self) -> None:
        candles = [mk(i, "1", "2", "1", "1") for i in (0, 1, 3, 4, 5, 6, 8)]

        run, holes = longest_contiguous(candles, Timeframe.H1)

        assert [c.open_time for c in run] == [T0 + i * H for i in (3, 4, 5, 6)]
        assert holes == 2

    def test_cache_round_trip_keeps_decimals_exactly(self, tmp_path: Path) -> None:
        candles = [mk(i, "100.12345678", "101.5", "99.25", "100.5", "12.3456789") for i in range(3)]
        path = cache_path(tmp_path / "sub", "BTC/USDT", Timeframe.H1)

        write_cache(path, candles)

        assert path.name == "BTCUSDT_1h.csv"
        assert read_cache(path, "BTC/USDT", Timeframe.H1) == candles
        assert read_cache(tmp_path / "none.csv", "BTC/USDT", Timeframe.H1) == []
