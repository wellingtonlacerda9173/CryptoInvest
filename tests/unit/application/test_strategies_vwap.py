from datetime import timedelta

from engine.application.strategies import VwapReclaim
from engine.domain.candle import Candle
from engine.domain.types import Timeframe
from tests.unit.application.helpers import T0, Row, build_candles, candle_row, wave_rows
from tests.unit.application.strategy_series import CTX


def session_vwap_by_hand(rows: list[Row]) -> float:
    price_volume = volume = 0.0
    for _, high, low, close, vol in rows:
        price_volume += (high + low + close) / 3 * vol
        volume += vol
    return price_volume / volume


def bounce_rows(*, low_offset: float = -0.02, bullish: bool = True) -> list[Row]:
    rows, _ = wave_rows(60)
    vwap = session_vwap_by_hand(rows)
    open_ = vwap + max(0.4, low_offset + 0.05)
    last: Row = (open_, vwap + 0.8, vwap + low_offset, vwap + (0.7 if bullish else 0.3), 200.0)
    if not bullish:
        last = (vwap + 0.7, vwap + 0.8, vwap + low_offset, vwap + 0.3, 200.0)
    return [*rows, candle_row(vwap + 0.1, vwap + 0.5, 120, 0.1), last]


def bounce_candles(**kwargs: float | bool) -> list[Candle]:
    return build_candles(bounce_rows(**kwargs))  # type: ignore[arg-type]


class TestVwapReclaim:
    def test_bounce_from_above_is_a_trigger(self) -> None:
        result = VwapReclaim().evaluate(bounce_candles(), CTX)

        assert result is not None
        assert result.triggers == ("rejeicao_no_vwap", "sessao_com_amostra_minima")

    def test_bounce_needs_a_bullish_candle(self) -> None:
        assert VwapReclaim().evaluate(bounce_candles(bullish=False), CTX) is None

    def test_bounce_needs_the_low_to_touch_vwap(self) -> None:
        assert VwapReclaim().evaluate(bounce_candles(low_offset=0.45), CTX) is None

    def test_incomplete_session_in_data_is_rejected(self) -> None:
        late_start = build_candles(bounce_rows(), start=T0 + timedelta(hours=1))
        assert VwapReclaim().evaluate(late_start, CTX) is None

    def test_first_candles_after_utc_midnight_are_rejected(self) -> None:
        rows = bounce_rows()
        start = T0 + timedelta(minutes=15 * 3) - len(rows) * timedelta(minutes=15)
        candles = build_candles(rows, start=start)
        assert candles[-1].open_time.hour == 0
        assert VwapReclaim().evaluate(candles, CTX) is None

    def test_daily_timeframe_has_no_session(self) -> None:
        daily = build_candles(bounce_rows(), timeframe=Timeframe.D1)
        assert VwapReclaim().evaluate(daily, CTX) is None
