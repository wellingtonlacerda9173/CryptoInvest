from datetime import UTC, datetime
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import TypeAdapter, ValidationError

from engine.domain.order import EntryOrder, ExitOrder, Order, OrderType
from engine.domain.signal import Signal
from engine.domain.types import Timeframe

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def signal(**overrides: object) -> Signal:
    data: dict[str, object] = {
        "symbol": "BTC/USDT",
        "timeframe": Timeframe.H1,
        "created_at": NOW,
        "strategy": "test",
        "entry": Decimal(100),
        "stop_loss": Decimal(95),
        "take_profit": Decimal(110),
    }
    return Signal(**(data | overrides))  # type: ignore[arg-type]


def test_signal_with_min_reward_risk_is_valid() -> None:
    assert signal().reward_risk == 2


def test_signal_below_default_reward_risk_rejected() -> None:
    with pytest.raises(ValidationError, match="reward/risk"):
        signal(take_profit=Decimal(109))


def test_signal_custom_min_reward_risk_can_only_be_stricter() -> None:
    assert signal(take_profit=Decimal(115), min_reward_risk=Decimal("2.5")).reward_risk == 3
    with pytest.raises(ValidationError):
        signal(take_profit=Decimal(112), min_reward_risk=Decimal(3))
    with pytest.raises(ValidationError):
        signal(take_profit=Decimal(108), min_reward_risk=Decimal("1.5"))


@pytest.mark.parametrize("stop", [Decimal(100), Decimal(101)])
def test_stop_must_be_below_entry(stop: Decimal) -> None:
    with pytest.raises(ValidationError, match="stop_loss"):
        signal(stop_loss=stop)


def test_signal_requires_stop_and_target() -> None:
    with pytest.raises(ValidationError):
        Signal(symbol="BTC/USDT", timeframe=Timeframe.H1, created_at=NOW, strategy="x", entry=Decimal(1))  # type: ignore[call-arg]


def test_signal_has_no_leverage_or_short_fields() -> None:
    fields = set(Signal.model_fields) | set(EntryOrder.model_fields) | set(ExitOrder.model_fields)
    assert not fields & {"leverage", "margin", "futures", "short"}


@given(
    entry=st.decimals(min_value=1, max_value=1000, places=2),
    risk=st.decimals(min_value="0.01", max_value=500, places=2),
    reward=st.decimals(min_value="0.01", max_value=2000, places=2),
)
def test_accepted_signal_always_respects_levels_and_ratio(
    entry: Decimal, risk: Decimal, reward: Decimal
) -> None:
    try:
        sig = signal(entry=entry, stop_loss=entry - risk, take_profit=entry + reward)
    except ValidationError:
        assert entry - risk <= 0 or reward / risk < 2
    else:
        assert sig.stop_loss < sig.entry < sig.take_profit
        assert sig.reward_risk >= 2


def entry_order(**overrides: object) -> EntryOrder:
    data: dict[str, object] = {
        "client_order_id": "abc",
        "symbol": "BTC/USDT",
        "type": OrderType.LIMIT,
        "quantity": Decimal("0.1"),
        "price": Decimal(100),
        "stop_loss": Decimal(95),
        "take_profit": Decimal(110),
        "created_at": NOW,
    }
    return EntryOrder(**(data | overrides))  # type: ignore[arg-type]


def test_entry_order_cannot_exist_without_stop_loss() -> None:
    with pytest.raises(ValidationError, match="stop_loss"):
        EntryOrder(  # type: ignore[call-arg]
            client_order_id="abc",
            symbol="BTC/USDT",
            type=OrderType.LIMIT,
            quantity=Decimal(1),
            price=Decimal(100),
            take_profit=Decimal(110),
            created_at=NOW,
        )


def test_entry_order_rejects_none_stop_loss() -> None:
    with pytest.raises(ValidationError):
        entry_order(stop_loss=None)


@pytest.mark.parametrize("stop", [Decimal(100), Decimal(120)])
def test_entry_order_stop_must_be_below_price(stop: Decimal) -> None:
    with pytest.raises(ValidationError, match="stop_loss"):
        entry_order(stop_loss=stop)


def test_entry_order_from_signal_carries_levels() -> None:
    order = EntryOrder.from_signal(signal(), client_order_id="x1", quantity=Decimal(2))
    assert (order.price, order.stop_loss, order.take_profit) == (Decimal(100), Decimal(95), Decimal(110))


def test_entry_order_is_frozen() -> None:
    with pytest.raises(ValidationError):
        entry_order().stop_loss = Decimal(1)  # type: ignore[misc]


def test_sell_limit_requires_price() -> None:
    with pytest.raises(ValidationError, match="price"):
        ExitOrder(
            client_order_id="a", symbol="BTC/USDT", type=OrderType.LIMIT, quantity=Decimal(1), created_at=NOW
        )


def test_order_union_discriminates_by_side() -> None:
    adapter: TypeAdapter[EntryOrder | ExitOrder] = TypeAdapter(Order)
    parsed = adapter.validate_python(
        {
            "side": "sell",
            "client_order_id": "a",
            "symbol": "BTC/USDT",
            "type": "market",
            "quantity": "1",
            "created_at": NOW,
        }
    )
    assert isinstance(parsed, ExitOrder)
    with pytest.raises(ValidationError):
        adapter.validate_python(
            {
                "side": "buy",
                "client_order_id": "a",
                "symbol": "BTC/USDT",
                "type": "market",
                "quantity": "1",
                "created_at": NOW,
            }
        )
