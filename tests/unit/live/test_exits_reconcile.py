from decimal import Decimal

import pytest

from engine.application.risk import ManagedPosition
from engine.domain.order import ExitOrder, OrderStatus, OrderType
from engine.infrastructure.live.errors import FilterViolationError, TradingHaltedError
from tests.unit.live.support import NOW, SYMBOL, Rig, approved_entry

D = Decimal


def exit_order(qty: str = "10", type_: OrderType = OrderType.MARKET, price: str | None = None) -> ExitOrder:
    return ExitOrder(
        client_order_id="x-1",
        symbol=SYMBOL,
        type=type_,
        quantity=D(qty),
        price=D(price) if price else None,
        created_at=NOW,
    )


async def protected(rig: Rig) -> None:
    await rig.gateway.place_order(approved_entry())
    rig.fake.set_price("BTCUSDT", D(99))
    await rig.gateway.poll()
    rig.fake.set_price("BTCUSDT", D(103))
    rig.fake.reset_log()


def local_position(qty: str = "10", stop: str = "95") -> ManagedPosition:
    return ManagedPosition(
        symbol=SYMBOL,
        entry_price=D(99),
        initial_quantity=D(10),
        quantity=D(qty),
        initial_stop=D(95),
        stop=D(stop),
        take_profit=D(111),
        highest_price=D(99),
        opened_at=NOW,
    )


async def test_market_exit_cancels_protection_only_to_sell_and_closes(rig: Rig) -> None:
    await protected(rig)

    report = await rig.gateway.place_order(exit_order())

    assert report.status is OrderStatus.FILLED
    assert rig.gateway.entry_state("e-1") == "closed"
    assert rig.fake.free("BTC") == 0
    assert rig.fake.uncovered_base() == 0
    assert [f.kind.value for f in rig.gateway.fills()] == ["entry", "exit"]


async def test_partial_exit_rearms_protection_for_remainder(rig: Rig) -> None:
    await protected(rig)

    await rig.gateway.place_order(exit_order("4"))

    assert rig.gateway.protection_of("e-1") == (D(6), D(95), D(111))
    assert rig.fake.uncovered_base() == 0
    assert [s.qty for s in rig.fake.active_stops()] == [D(6)]


async def test_unfilled_limit_exit_is_ioc_and_never_leaves_position_unprotected(rig: Rig) -> None:
    await protected(rig)

    report = await rig.gateway.place_order(exit_order("10", OrderType.LIMIT, "109"))

    assert report.status is OrderStatus.EXPIRED
    assert rig.gateway.protection_of("e-1") == (D(10), D(95), D(111))
    assert rig.fake.uncovered_base() == 0


async def test_exit_failure_still_rearms_protection(rig: Rig) -> None:
    await protected(rig)
    rig.fake.fail("/api/v3/order", method="POST", times=1, kind="binance", status=400, code=-1013)

    with pytest.raises(Exception, match="code=-1013"):
        await rig.gateway.place_order(exit_order())

    assert rig.gateway.protection_of("e-1") == (D(10), D(95), D(111))
    assert rig.fake.uncovered_base() == 0


async def test_exit_larger_than_position_is_refused(rig: Rig) -> None:
    await protected(rig)

    with pytest.raises(FilterViolationError):
        await rig.gateway.place_order(exit_order("11"))

    assert rig.fake.writes() == []


async def test_reconciliation_ok_does_not_halt_or_write(rig: Rig) -> None:
    await protected(rig)

    result = await rig.gateway.reconcile([local_position()])

    assert result.ok
    assert not rig.gateway.halted
    assert rig.fake.writes() == []


async def test_balance_divergence_halts_and_never_touches_stops(rig: Rig) -> None:
    await protected(rig)
    rig.fake.balances["BTC"][0] += D(3)

    result = await rig.gateway.reconcile([local_position()])

    assert not result.ok
    assert rig.gateway.halted
    assert rig.fake.writes() == []
    assert [s.stop for s in rig.fake.active_stops()] == [D(95)]
    with pytest.raises(TradingHaltedError):
        await rig.gateway.place_order(approved_entry(cid="e-2"))


async def test_stop_missing_on_exchange_is_a_divergence(rig: Rig) -> None:
    await protected(rig)
    for order in rig.fake.active_stops():
        order.status = "CANCELED"

    result = await rig.gateway.reconcile([local_position()])

    assert not result.ok
    assert rig.gateway.halted
    assert any("stop" in text for text in (result.describe(),))


async def test_unknown_open_order_and_unknown_trade_halt(rig: Rig) -> None:
    await rig.gateway.place_order(approved_entry())
    rig.fake.trades.append(
        {
            "symbol": "BTCUSDT",
            "id": 999,
            "orderId": 77,
            "orderListId": -1,
            "price": "100",
            "qty": "1",
            "quoteQty": "100",
            "commission": "0",
            "commissionAsset": "BNB",
            "time": 4_102_444_800_000,
            "isBuyer": True,
            "isMaker": True,
            "isBestMatch": True,
        }
    )

    result = await rig.gateway.reconcile([])

    assert result.unknown_trades == (999,)
    assert rig.gateway.halted


async def test_fills_are_append_only_idempotent_and_keep_real_commission(rig: Rig) -> None:
    await protected(rig)
    await rig.gateway.poll()
    await rig.gateway.poll()

    fills = rig.gateway.fills()

    assert len(fills) == 1
    assert (fills[0].commission_asset, fills[0].sequence) == ("BNB", 1)
    assert fills[0].commission > 0
    with pytest.raises(Exception, match=r"frozen|immutable"):
        fills[0].commission = D(0)  # type: ignore[misc]
