from decimal import Decimal

import pytest

from engine.application.backtest import HypotheticalStatus, default_rules, simulate_signal
from engine.application.risk.sizing import floor_to_step
from engine.domain.signal import Signal
from engine.domain.types import Timeframe
from tests.unit.backtest.engine_helpers import ENTRY_CANDLE, QUIET, T0, flat_then, settings

D = Decimal
RULES = default_rules("BTC/USDT")
SLIP = D("0.0005")
CONFIG = settings(hypothetical_horizon=4)
SIGNAL = Signal(
    symbol="BTC/USDT",
    timeframe=Timeframe.H1,
    created_at=T0,
    strategy="t",
    entry=D(100),
    stop_loss=D(95),
    take_profit=D(111),
)


def outcome(rows: list[tuple[str, str, str, str]]):  # type: ignore[no-untyped-def]
    candles = flat_then(rows)
    return simulate_signal(
        SIGNAL, candles, 29, equity=D(10_000), settings=CONFIG, rules=default_rules("BTC/USDT")
    )


def test_stop_on_the_entry_candle_is_a_loss_at_the_stop_with_slippage() -> None:
    result = outcome([("100", "101", "94", "95")])

    assert result.status is HypotheticalStatus.STOP
    assert result.r_multiple < -1
    assert result.candles == 0


def test_gap_through_the_stop_exits_at_the_open() -> None:
    clean = outcome([ENTRY_CANDLE, ("100", "101", "94", "95")])
    gap = outcome([ENTRY_CANDLE, ("90", "91", "89", "90")])

    assert gap.status is HypotheticalStatus.STOP
    assert gap.pnl < clean.pnl < 0


def test_stop_and_target_on_the_same_candle_counts_the_stop() -> None:
    assert outcome([ENTRY_CANDLE, ("100", "120", "90", "100")]).status is HypotheticalStatus.STOP


def test_target_needs_a_candle_after_the_entry_candle() -> None:
    assert outcome([("100", "120", "99.5", "119"), QUIET]).status is not HypotheticalStatus.TARGET
    assert outcome([ENTRY_CANDLE, ("100", "120", "99", "119")]).status is HypotheticalStatus.TARGET


def test_time_limit_closes_at_the_last_close_with_slippage() -> None:
    result = outcome([ENTRY_CANDLE, *[("100", "102.5", "99", "102")] * 5])

    assert result.status is HypotheticalStatus.TIMEOUT
    assert result.candles == 4
    expected_exit = D(102) * (1 - SLIP)
    risk = D(10_000) * D("0.005")
    quantity = floor_to_step(risk / (D(5) + D("0.001") * D(195)), D("0.00001"))
    assert result.pnl == (expected_exit - 100) * quantity - 100 * quantity * D(
        "0.001"
    ) - expected_exit * quantity * D("0.001")


def test_data_ending_before_resolution_is_unresolved_not_invented() -> None:
    assert outcome([ENTRY_CANDLE, QUIET]).status is HypotheticalStatus.UNRESOLVED
    assert outcome([]).status is HypotheticalStatus.UNRESOLVED


@pytest.mark.parametrize("rows", [[("101", "102", "100.5", "101")] * 3])
def test_limit_never_touched_within_validity_is_not_filled(rows: list[tuple[str, str, str, str]]) -> None:
    result = outcome(rows)

    assert result.status is HypotheticalStatus.NOT_FILLED
    assert result.pnl == 0
