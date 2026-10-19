from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from engine.domain.candle import Candle
from engine.domain.order import EntryOrder, OrderType, revalidate_order
from engine.domain.position import Position
from engine.domain.risk import MAX_DAILY_LOSS, MAX_RISK_PER_TRADE, RiskConfig
from engine.domain.rules import DEFAULT_MIN_REWARD_RISK, meets_reward_risk
from engine.domain.signal import Signal
from engine.domain.types import Timeframe

NOW = datetime(2026, 1, 1, tzinfo=UTC)
money = st.decimals(min_value="0.01", max_value=10_000, places=2)
ratio = st.decimals(min_value="-1", max_value="2", places=4)


def build_order(price: Decimal, stop: Decimal, target: Decimal) -> EntryOrder:
    return EntryOrder(
        client_order_id="o1",
        symbol="BTC/USDT",
        type=OrderType.LIMIT,
        quantity=Decimal(1),
        price=price,
        stop_loss=stop,
        take_profit=target,
        created_at=NOW,
    )


@given(price=money, stop=money, target=money)
def test_entry_order_exists_only_with_stop_below_target_above_and_min_rr(
    price: Decimal, stop: Decimal, target: Decimal
) -> None:
    try:
        order = build_order(price, stop, target)
    except ValidationError:
        assert not (stop < price < target and meets_reward_risk(price, stop, target, DEFAULT_MIN_REWARD_RISK))
    else:
        assert order.stop_loss < order.price < order.take_profit
        assert order.take_profit - order.price >= 2 * (order.price - order.stop_loss)


@given(price=money, stop=money, target=money, new_stop=st.none() | money)
def test_model_copy_never_yields_an_invalid_order(
    price: Decimal, stop: Decimal, target: Decimal, new_stop: Decimal | None
) -> None:
    try:
        order = build_order(price, stop, target)
    except ValidationError:
        return
    try:
        copy = order.model_copy(update={"stop_loss": new_stop})
    except ValidationError:
        return
    assert copy.stop_loss < copy.price
    assert meets_reward_risk(copy.price, copy.stop_loss, copy.take_profit, DEFAULT_MIN_REWARD_RISK)
    assert revalidate_order(copy) == copy


def test_revalidate_order_rejects_tampered_instance() -> None:
    order = build_order(Decimal(100), Decimal(95), Decimal(110))
    object.__setattr__(order, "stop_loss", Decimal(120))

    with pytest.raises(ValidationError):
        revalidate_order(order)


@given(
    entry=money,
    risk=money,
    reward=money,
    minimum=st.decimals(min_value=0, max_value=5, places=2),
)
def test_signal_never_below_two_to_one(
    entry: Decimal, risk: Decimal, reward: Decimal, minimum: Decimal
) -> None:
    try:
        signal = Signal(
            symbol="BTC/USDT",
            timeframe=Timeframe.H1,
            created_at=NOW,
            strategy="s",
            entry=entry,
            stop_loss=entry - risk,
            take_profit=entry + reward,
            min_reward_risk=minimum,
        )
    except ValidationError:
        return
    assert signal.min_reward_risk >= 2
    assert signal.take_profit - signal.entry >= 2 * (signal.entry - signal.stop_loss)


@given(
    risk=ratio,
    daily=ratio,
    fraction=ratio,
    min_rr=st.decimals(min_value="0", max_value=5, places=2),
    positions=st.integers(-5, 50),
)
def test_risk_config_respects_hard_ceilings(
    risk: Decimal, daily: Decimal, fraction: Decimal, min_rr: Decimal, positions: int
) -> None:
    try:
        cfg = RiskConfig(
            risk_per_trade=risk,
            max_daily_loss=daily,
            max_position_fraction=fraction,
            min_reward_risk=min_rr,
            max_open_positions=positions,
        )
    except ValidationError:
        return
    assert 0 < cfg.risk_per_trade <= MAX_RISK_PER_TRADE
    assert cfg.risk_per_trade <= cfg.max_daily_loss <= MAX_DAILY_LOSS
    assert 0 < cfg.max_position_fraction <= 1
    assert cfg.min_reward_risk >= 2
    assert 1 <= cfg.max_open_positions <= 2


@given(value=st.decimals(min_value="-1", max_value="5", places=4))
def test_risk_config_model_copy_revalidates(value: Decimal) -> None:
    try:
        cfg = RiskConfig().model_copy(update={"risk_per_trade": value})
    except ValidationError:
        assert not (0 < value <= MAX_RISK_PER_TRADE)
    else:
        assert cfg.risk_per_trade <= MAX_RISK_PER_TRADE


@given(entry=money, stop=money, target=st.none() | money)
def test_position_stop_always_below_entry(entry: Decimal, stop: Decimal, target: Decimal | None) -> None:
    try:
        pos = Position(
            symbol="BTC/USDT",
            quantity=Decimal(1),
            entry_price=entry,
            stop_loss=stop,
            take_profit=target,
            opened_at=NOW,
        )
    except ValidationError:
        return
    assert pos.stop_loss < pos.entry_price
    assert pos.take_profit is None or pos.take_profit > pos.entry_price


@given(o=money, h=money, low=money, c=money)
def test_candle_ohlc_invariant(o: Decimal, h: Decimal, low: Decimal, c: Decimal) -> None:
    try:
        candle = Candle(
            symbol="BTC/USDT",
            timeframe=Timeframe.M1,
            open_time=NOW,
            open=o,
            high=h,
            low=low,
            close=c,
            volume=Decimal(1),
            closed=True,
        )
    except ValidationError:
        assert h < max(o, c) or low > min(o, c)
    else:
        assert candle.low <= min(candle.open, candle.close)
        assert candle.high >= max(candle.open, candle.close)
        assert candle.close_time == NOW + timedelta(minutes=1)


@pytest.mark.parametrize("field", ["entry", "stop_loss", "take_profit"])
def test_float_prices_are_rejected(field: str) -> None:
    data: dict[str, object] = {
        "symbol": "BTC/USDT",
        "timeframe": Timeframe.H1,
        "created_at": NOW,
        "strategy": "s",
        "entry": Decimal(100),
        "stop_loss": Decimal(90),
        "take_profit": Decimal(120),
    }
    data[field] = 0.1 + 0.2

    with pytest.raises(ValidationError, match="float"):
        Signal(**data)  # type: ignore[arg-type]


def test_model_construct_cannot_skip_validation() -> None:
    with pytest.raises(ValidationError):
        RiskConfig.model_construct(risk_per_trade=Decimal(1))
