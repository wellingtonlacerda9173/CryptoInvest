from decimal import Decimal

import pytest

from engine.domain.gateway import UnknownOrderError
from engine.infrastructure.live.errors import (
    ProtectionError,
    ProtectiveOrderCancelError,
    ReadOnlyModeError,
)
from tests.fakes.fake_binance import FakeBinance
from tests.unit.live.support import SYMBOL, Rig, approved_entry, build_rig

D = Decimal


async def protected(rig: Rig, price: str = "103") -> None:
    await rig.gateway.place_order(approved_entry())
    rig.fake.set_price("BTCUSDT", D(99))
    await rig.gateway.poll()
    rig.fake.set_price("BTCUSDT", D(price))
    rig.fake.reset_log()


async def test_raise_stop_replaces_oco_with_higher_stop(rig: Rig) -> None:
    await protected(rig)

    report = await rig.gateway.raise_stop("e-1", D("97"))

    assert report.client_order_id == "e-1:oco"
    assert [(s.qty, s.stop) for s in rig.fake.active_stops()] == [(D(10), D(97))]
    assert rig.gateway.protection_of("e-1") == (D(10), D(97), D(111))
    assert rig.fake.uncovered_base() == 0
    paths = [(r.method, r.path) for r in rig.fake.writes()]
    assert paths == [("DELETE", "/api/v3/orderList"), ("POST", "/api/v3/orderList/oco")]


@pytest.mark.parametrize("new_stop", ["95", "94", "90"])
async def test_raise_stop_never_lowers_or_repeats_stop(rig: Rig, new_stop: str) -> None:
    await protected(rig)

    with pytest.raises(ValueError, match="only be raised"):
        await rig.gateway.raise_stop("e-1", D(new_stop))

    assert rig.fake.writes() == []
    assert [s.stop for s in rig.fake.active_stops()] == [D(95)]


@pytest.mark.parametrize("new_stop", ["103", "102.95", "111", "120"])
async def test_raise_stop_refuses_stop_that_would_trigger_now_or_pass_target(rig: Rig, new_stop: str) -> None:
    await protected(rig)

    with pytest.raises(ValueError, match="below target and the current bid"):
        await rig.gateway.raise_stop("e-1", D(new_stop))

    assert rig.fake.writes() == []
    assert rig.fake.uncovered_base() == 0


async def test_raise_stop_replace_failure_sells_at_market(rig: Rig) -> None:
    await protected(rig)
    rig.fake.fail("/api/v3/orderList/oco", times=3, kind="binance", status=400, code=-1000)

    await rig.gateway.raise_stop("e-1", D("97"))

    assert rig.gateway.entry_state("e-1") == "closed"
    assert rig.fake.free("BTC") == 0
    assert rig.fake.uncovered_base() == 0
    assert any("emergencia" in m for _, m in rig.alerts)


async def test_raise_stop_window_race_triggers_emergency_sale(rig: Rig) -> None:
    await protected(rig)
    rig.fake.fail("/api/v3/orderList/oco", times=1, kind="binance", status=400, code=-2021)

    await rig.gateway.raise_stop("e-1", D("97"))

    assert rig.gateway.entry_state("e-1") == "closed"
    assert rig.fake.uncovered_base() == 0


async def test_raise_stop_cancel_failure_keeps_old_protection(rig: Rig) -> None:
    await protected(rig)
    rig.fake.fail("/api/v3/orderList", method="DELETE", kind="binance", status=400, code=-1000)

    with pytest.raises(Exception, match="code=-1000"):
        await rig.gateway.raise_stop("e-1", D("97"))

    assert [s.stop for s in rig.fake.active_stops()] == [D(95)]
    assert rig.gateway.protection_of("e-1") == (D(10), D(95), D(111))


async def test_raise_stop_cancel_unconfirmed_keeps_protection(rig: Rig) -> None:
    await protected(rig)
    rig.fake.fail("/api/v3/orderList", method="DELETE", kind="timeout")

    with pytest.raises(ProtectionError):
        await rig.gateway.raise_stop("e-1", D("97"))

    assert [s.stop for s in rig.fake.active_stops()] == [D(95)]
    assert rig.gateway.protection_of("e-1") == (D(10), D(95), D(111))


async def test_raise_stop_cancel_response_lost_still_replaces(rig: Rig) -> None:
    await protected(rig)
    rig.fake.fail("/api/v3/orderList", method="DELETE", after=True)

    await rig.gateway.raise_stop("e-1", D("97"))

    assert [s.stop for s in rig.fake.active_stops()] == [D(97)]
    assert rig.fake.uncovered_base() == 0


async def test_raise_stop_unknown_entry(rig: Rig) -> None:
    with pytest.raises(UnknownOrderError):
        await rig.gateway.raise_stop("nope", D(97))


async def test_drop_target_keeps_stop_only_and_stop_can_still_rise(rig: Rig) -> None:
    await protected(rig)

    await rig.gateway.drop_target("e-1")

    assert rig.gateway.protection_of("e-1") == (D(10), D(95), None)
    assert [s.stop for s in rig.fake.active_stops()] == [D(95)]
    await rig.gateway.raise_stop("e-1", D(97))
    assert [s.stop for s in rig.fake.active_stops()] == [D(97)]
    assert rig.fake.uncovered_base() == 0


async def test_cancel_of_protective_orders_is_refused(rig: Rig) -> None:
    await protected(rig)

    for cid in ("e-1:oco", rig.gateway.protection_ids("e-1")[0]):
        with pytest.raises(ProtectiveOrderCancelError):
            await rig.gateway.cancel_order(SYMBOL, cid)

    assert rig.fake.writes() == []
    assert len(rig.fake.active_stops()) == 1


async def test_cancel_of_filled_entry_never_cancels_its_protection(rig: Rig) -> None:
    await protected(rig)

    report = await rig.gateway.cancel_order(SYMBOL, "e-1")

    assert report.status.value == "filled"
    assert rig.fake.writes() == []
    assert rig.fake.uncovered_base() == 0


async def test_read_only_blocks_raise_stop(fake: FakeBinance) -> None:
    writer = build_rig(fake)
    await protected(writer)
    reader = build_rig(fake, read_only=True)
    reader.gateway._entries = writer.gateway._entries

    with pytest.raises(ReadOnlyModeError):
        await reader.gateway.raise_stop("e-1", D(97))

    assert fake.writes() == []
    await writer.client.aclose()
    await reader.client.aclose()
