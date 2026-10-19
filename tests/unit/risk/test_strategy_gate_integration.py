"""Integracao estrategia -> evaluate_entry -> open_position -> manage_position -> record_trade."""

from datetime import timedelta
from decimal import Decimal

import pytest

from engine.application.risk import (
    ApprovedEntry,
    EventCalendar,
    ManagedPosition,
    MarketSnapshot,
    RiskState,
    SymbolRules,
    evaluate_entry,
    manage_position,
    open_position,
)
from engine.application.risk.sizing import net_reward_risk_ok
from engine.application.strategies import ElephantBarBreakout, EmaCrossTrend, LevelsConfig, StrategyContext
from engine.application.strategies.levels import compute_levels
from engine.domain.candle import Candle
from engine.domain.risk import RiskConfig
from engine.domain.trade import ExitReason
from tests.unit.application.helpers import build_candles, reversal_rows
from tests.unit.application.test_strategies import breakout_series

D = Decimal
CONFIG = RiskConfig()
RULES = SymbolRules(
    symbol="BTC/USDT", tick_size=D("0.01"), step_size=D("0.001"), min_quantity=D("0.001"), min_notional=D(10)
)


def _elephant() -> tuple[list[Candle], object, StrategyContext]:
    return breakout_series(), ElephantBarBreakout(), StrategyContext(taker_fee_rate=CONFIG.taker_fee_rate)


def _ema_cross() -> tuple[list[Candle], object, StrategyContext]:
    candles = build_candles(reversal_rows())
    context = StrategyContext(htf_trend="up")
    strategy = EmaCrossTrend()
    for end in range(40, len(candles) + 1):
        if strategy.evaluate(candles[:end], context) is not None:
            return candles[:end], strategy, context
    raise AssertionError("EmaCrossTrend nao gerou sinal na serie de teste")


def _approve(candles: list[Candle], strategy: object, context: StrategyContext) -> ApprovedEntry:
    result = strategy.evaluate(candles, context)  # type: ignore[attr-defined]
    assert result is not None
    signal = result.signal
    now = candles[-1].close_time + timedelta(seconds=5)
    outcome = evaluate_entry(
        signal,
        state=RiskState.initial(D(10_000), now),
        config=CONFIG,
        rules=RULES,
        positions=[],
        pending=[],
        market=MarketSnapshot(
            symbol=signal.symbol,
            at=now,
            bid=signal.entry - D("0.01"),
            ask=signal.entry + D("0.01"),
            quote_volume_24h=D(5_000_000),
        ),
        history=candles,
        calendar=EventCalendar(
            loaded_at=now - timedelta(days=1),
            valid_until=now + timedelta(days=30),
            acknowledged_no_events=True,
        ),
        now=now,
        client_order_id="int-1",
    )
    assert isinstance(outcome, ApprovedEntry), outcome
    return outcome


@pytest.mark.parametrize("scenario", [_elephant, _ema_cross], ids=["elephant_bar", "ema_cross"])
class TestStrategySignalsPassTheGate:
    def test_valid_signal_is_approved_with_default_risk_config(self, scenario) -> None:  # type: ignore[no-untyped-def]
        candles, strategy, context = scenario()

        approved = _approve(candles, strategy, context)

        sig = approved.signal
        assert sig.reward_risk >= 2
        assert net_reward_risk_ok(
            approved.order.price,
            approved.order.stop_loss,
            approved.order.take_profit,
            CONFIG.taker_fee_rate,
            CONFIG.min_reward_risk,
        )

    def test_net_pnl_at_target_is_at_least_twice_the_approved_risk(self, scenario) -> None:  # type: ignore[no-untyped-def]
        candles, strategy, context = scenario()
        approved = _approve(candles, strategy, context)
        order = approved.order
        opened_at = approved.guards.approved_at
        entry_fee = CONFIG.taker_fee_rate * order.price * order.quantity

        position = open_position(approved, opened_at=opened_at, fee=entry_fee)
        assert isinstance(position, ManagedPosition)
        target_candle = Candle(
            symbol=order.symbol,
            timeframe=candles[-1].timeframe,
            open_time=opened_at + timedelta(minutes=15),
            open=order.price,
            high=order.take_profit,
            low=order.price - (order.price - order.stop_loss) / 2,
            close=order.take_profit,
            volume=D(1),
            closed=True,
        )
        managed = manage_position(position, target_candle, RULES, CONFIG)

        [fill] = managed.exits
        assert (fill.reason, fill.price) == (ExitReason.TAKE_PROFIT, order.take_profit)
        trade = fill.to_trade(position)
        risk_share = approved.risk_amount * fill.quantity / order.quantity
        assert trade.pnl >= 2 * risk_share

        state = RiskState.initial(D(10_000), opened_at).record_trade(trade, target_candle.close_time, CONFIG)
        assert state.equity == D(10_000) + trade.pnl
        assert state.ledger == (trade,)


class TestComputeLevelsNetTarget:
    ENTRY = D("100")

    def test_target_is_the_smallest_tick_meeting_the_net_reward_risk(self) -> None:
        stop, target = compute_levels(self.ENTRY, 2.0, D(98), LevelsConfig(), CONFIG.taker_fee_rate) or (
            D(0),
            D(0),
        )

        assert net_reward_risk_ok(self.ENTRY, stop, target, CONFIG.taker_fee_rate, D(2))
        assert not net_reward_risk_ok(self.ENTRY, stop, target - D("0.00000002"), CONFIG.taker_fee_rate, D(2))

    def test_higher_fee_pushes_the_target_further(self) -> None:
        low = compute_levels(self.ENTRY, 2.0, D(98), LevelsConfig(), D("0.001"))
        high = compute_levels(self.ENTRY, 2.0, D(98), LevelsConfig(), D("0.005"))

        assert low is not None
        assert high is not None
        assert high[1] > low[1]

    def test_context_fee_overrides_the_levels_config_fee(self) -> None:
        candles = breakout_series()
        default = ElephantBarBreakout().evaluate(candles, StrategyContext())
        pricier = ElephantBarBreakout().evaluate(candles, StrategyContext(taker_fee_rate=D("0.005")))

        assert default is not None
        assert pricier is not None
        assert pricier.signal.take_profit > default.signal.take_profit
