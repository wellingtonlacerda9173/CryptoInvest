from datetime import timedelta
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from engine.application.risk import ManagedPosition, ManagementResult, close_position, manage_position
from engine.domain.candle import Candle
from engine.domain.risk import RiskConfig
from engine.domain.trade import ExitReason
from engine.domain.types import Timeframe
from tests.unit.risk.factories import NOW, SYMBOL, D, candle, position, rules

CFG = RiskConfig(taker_fee_rate=D(0))
RULES = rules()


def run(pos: ManagedPosition, *bars: Candle) -> ManagementResult:
    result = ManagementResult(position=pos)
    for bar in bars:
        result = manage_position(pos, bar, RULES, CFG)
        if result.position is None:
            return result
        pos = result.position
    return result


def test_breakeven_moves_stop_to_entry_at_one_r() -> None:
    result = manage_position(position(), candle("100", "105", "99", "104"), RULES, CFG)

    assert result.position is not None
    assert result.position.stop == D(100)
    assert result.position.breakeven_reached
    assert result.stop_update == D(100)
    assert not result.exits


def test_no_breakeven_below_one_r() -> None:
    result = manage_position(position(), candle("100", "104.99", "99", "104"), RULES, CFG)

    assert result.position is not None
    assert result.position.stop == D(95)
    assert result.stop_update is None


def test_partial_exit_at_first_target_then_trailing_on_remainder() -> None:
    result = manage_position(position(), candle("100", "110", "99", "109"), RULES, CFG)

    assert result.position is not None
    [fill] = result.exits
    assert (fill.quantity, fill.price, fill.reason) == (D("0.5"), D(110), ExitReason.TAKE_PROFIT)
    assert result.position.quantity == D("0.5")
    assert result.position.take_profit is None
    assert result.position.stop == D(105)
    assert result.position.realized_pnl == D(5)


def test_partial_rounds_down_to_step() -> None:
    result = manage_position(position(quantity="1.001"), candle("100", "110", "99", "109"), RULES, CFG)

    [fill] = result.exits
    assert fill.quantity == D("0.500")


def test_partial_skipped_when_it_would_break_minimums_but_trailing_starts() -> None:
    result = manage_position(position(quantity="0.001"), candle("100", "110", "99", "109"), RULES, CFG)

    assert result.position is not None
    assert not result.exits
    assert result.position.quantity == D("0.001")
    assert result.position.stop == D(105)


def test_trailing_only_rises() -> None:
    first = manage_position(position(), candle("100", "112", "99", "111"), RULES, CFG)
    assert first.position is not None
    assert first.position.stop == D(107)

    second = manage_position(first.position, candle("111", "111", "108", "110"), RULES, CFG)
    assert second.position is not None
    assert second.position.stop == D(107)
    assert second.stop_update is None


def test_profit_protection_exits_when_giving_back_forty_percent_of_peak() -> None:
    first = manage_position(position(target=None), candle("100", "110", "100", "109"), RULES, CFG)
    assert first.position is not None

    tie = manage_position(first.position, candle("109", "109", "100.5", "106"), RULES, CFG)

    assert tie.position is None
    [fill] = tie.exits
    assert (fill.reason, fill.price, fill.quantity) == (ExitReason.PROFIT_PROTECTION, D(106), D(1))


def test_profit_protection_not_triggered_just_above_threshold() -> None:
    first = manage_position(position(target=None), candle("100", "110", "100", "109"), RULES, CFG)
    assert first.position is not None

    result = manage_position(first.position, candle("109", "109", "100.5", "106.01"), RULES, CFG)

    assert result.position is not None


def test_profit_protection_is_configurable() -> None:
    cfg = RiskConfig(profit_giveback=D("0.2"), taker_fee_rate=D(0))
    first = manage_position(position(target=None), candle("100", "110", "100", "109"), RULES, cfg)
    assert first.position is not None

    result = manage_position(first.position, candle("109", "109", "100.5", "107.9"), RULES, cfg)

    assert result.position is None


def test_stop_wins_over_target_in_the_same_candle() -> None:
    result = manage_position(position(), candle("100", "115", "94", "110"), RULES, CFG)

    assert result.position is None
    [fill] = result.exits
    assert (fill.reason, fill.price) == (ExitReason.STOP_LOSS, D(95))


def test_touching_the_stop_exactly_exits() -> None:
    result = manage_position(position(), candle("100", "101", "95", "100"), RULES, CFG)

    assert result.position is None


def test_gap_below_stop_exits_at_open_not_at_stop() -> None:
    result = manage_position(position(), candle("90", "92", "89", "91"), RULES, CFG)

    [fill] = result.exits
    assert fill.price == D(90)


def test_loser_always_leaves_through_the_stop() -> None:
    result = run(
        position(),
        candle("100", "101", "97", "98"),
        candle("98", "99", "96", "96.5"),
        candle("96.5", "97", "94.9", "95.5"),
    )

    assert result.position is None
    assert result.exits[0].reason is ExitReason.STOP_LOSS
    assert result.exits[0].price == D(95)


def test_new_stop_applies_from_next_candle() -> None:
    result = manage_position(position(), candle("100", "106", "99.5", "104"), RULES, CFG)

    assert result.position is not None
    assert result.position.stop == D(100)


def test_wrong_symbol_is_refused() -> None:
    other = Candle(
        symbol="ETH/USDT",
        timeframe=Timeframe.H1,
        open_time=NOW,
        open=D(1),
        high=D(1),
        low=D(1),
        close=D(1),
        volume=D(1),
        closed=True,
    )
    with pytest.raises(ValueError, match="symbol"):
        manage_position(position(), other, RULES, CFG)


def test_stop_cannot_be_lowered_below_initial_by_any_copy() -> None:
    with pytest.raises(ValidationError):
        position().model_copy(update={"stop": D(94)})


def test_manual_close_becomes_immutable_trade() -> None:
    pos = position(quantity="2")
    fill = close_position(pos, D(103), NOW + timedelta(hours=1), ExitReason.MANUAL, fee=D(0))

    trade = fill.to_trade(pos)

    assert trade.pnl == D(6)
    with pytest.raises(ValidationError):
        trade.exit_price = D(1)  # type: ignore[misc]


prices = st.integers(min_value=60, max_value=160).map(lambda n: Decimal(n) / 2)


@st.composite
def candle_sequences(draw: st.DrawFn) -> list[Candle]:
    result = []
    for i in range(draw(st.integers(1, 40))):
        o, h, low, c = (draw(prices) for _ in range(4))
        result.append(
            Candle(
                symbol=SYMBOL,
                timeframe=Timeframe.H1,
                open_time=NOW + timedelta(hours=i),
                open=o,
                high=max(o, h, low, c),
                low=min(o, h, low, c),
                close=c,
                volume=D(1),
                closed=True,
            )
        )
    return result


@given(
    bars=candle_sequences(),
    giveback=st.sampled_from(["0.2", "0.4", "0.9"]),
    quantity=st.sampled_from(["0.001", "0.003", "1", "7.123"]),
)
def test_no_sequence_lowers_the_stop_or_leaves_a_position_unprotected(
    bars: list[Candle], giveback: str, quantity: str
) -> None:
    cfg = RiskConfig(profit_giveback=D(giveback), taker_fee_rate=D(0))
    pos = position(quantity=quantity)
    sold = D(0)

    for bar in bars:
        result = manage_position(pos, bar, RULES, cfg)
        sold += sum((fill.quantity for fill in result.exits), start=D(0))
        if bar.low <= pos.stop:
            assert result.position is None
            assert result.exits[0].reason is ExitReason.STOP_LOSS
            assert result.exits[0].price <= pos.stop
        if result.position is None:
            assert sold == pos.initial_quantity
            break
        new = result.position
        assert new.stop >= pos.stop >= new.initial_stop == pos.initial_stop
        assert new.quantity > 0
        assert new.quantity + sold == new.initial_quantity
        assert (result.stop_update is not None) == (new.stop > pos.stop or new.quantity != pos.quantity)
        pos = new
