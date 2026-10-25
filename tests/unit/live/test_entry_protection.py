from decimal import Decimal

import pytest

from engine.domain.order import OrderStatus
from tests.fakes.fake_binance import FakeBinance
from tests.unit.live.support import SYMBOL, Rig, approved_entry

D = Decimal


async def test_entry_rests_on_book_with_opoco_and_no_protection_needed_yet(rig: Rig) -> None:
    report = await rig.gateway.place_order(approved_entry())

    assert report.status is OrderStatus.NEW
    posts = [r.path for r in rig.fake.writes()]
    assert posts == ["/api/v3/orderList/opoco"]
    assert rig.fake.free("USDT") == D(9000)


async def test_filled_entry_gets_native_oco_atomically(rig: Rig) -> None:
    await rig.gateway.place_order(approved_entry())
    rig.fake.set_price("BTCUSDT", D(99))
    await rig.gateway.poll()

    stops = rig.fake.active_stops()
    assert [(s.qty, s.stop) for s in stops] == [(D(10), D(95))]
    assert rig.gateway.protection_of("e-1") == (D(10), D(95), D(111))
    assert rig.fake.uncovered_base() == 0
    assert [r.path for r in rig.fake.writes()] == ["/api/v3/orderList/opoco"]
    assert len(rig.gateway.fills()) == 1


async def test_fill_commission_in_base_shrinks_protected_quantity(fake: FakeBinance) -> None:
    from tests.unit.live.support import build_rig

    fake.commission_in_base = True
    rig = build_rig(fake)
    await rig.gateway.place_order(approved_entry())
    fake.set_price("BTCUSDT", D(99))
    await rig.gateway.poll()

    assert rig.gateway.protection_of("e-1") == (D("9.99"), D(95), D(111))
    assert fake.uncovered_base() == 0
    assert rig.gateway.fills()[0].commission_asset == "BTC"
    await rig.client.aclose()


@pytest.mark.parametrize("symbol", [SYMBOL])
async def test_stop_fill_closes_position_and_records_sale(rig: Rig, symbol: str) -> None:
    await rig.gateway.place_order(approved_entry())
    rig.fake.set_price("BTCUSDT", D(99))
    await rig.gateway.poll()
    rig.fake.set_price("BTCUSDT", D(94))
    await rig.gateway.poll()

    assert rig.gateway.entry_state("e-1") == "closed"
    assert [f.kind.value for f in rig.gateway.fills()] == ["entry", "stop"]
    assert rig.fake.uncovered_base() == 0
    assert rig.fake.free("BTC") == 0


async def _filled(rig: Rig, **kw: str) -> None:
    await rig.gateway.place_order(approved_entry(**kw))
    rig.fake.set_price("BTCUSDT", D(99))
    await rig.gateway.poll()


async def test_oco_activation_failure_after_fill_places_standalone_oco(rig: Rig) -> None:
    rig.fake.fail_activation = True
    await _filled(rig)

    assert rig.gateway.protection_of("e-1") == (D(10), D(95), D(111))
    assert rig.fake.uncovered_base() == 0
    assert rig.fake.writes()[-1].path == "/api/v3/orderList/oco"
    assert any(level == "critical" for level, _ in rig.alerts)


async def test_standalone_oco_failing_after_retries_sells_at_market(rig: Rig) -> None:
    rig.fake.fail_activation = True
    rig.fake.fail("/api/v3/orderList/oco", times=3, kind="binance", status=400, code=-1000)
    await _filled(rig)

    assert rig.gateway.entry_state("e-1") == "closed"
    assert rig.fake.free("BTC") == 0
    assert rig.fake.uncovered_base() == 0
    assert [f.kind.value for f in rig.gateway.fills()] == ["entry", "emergency"]


async def test_stop_that_would_trigger_immediately_sells_at_market(rig: Rig) -> None:
    rig.fake.hold_activation = True
    await _filled(rig)
    rig.fake.set_price("BTCUSDT", D(94))
    rig.fake.activate_pending("L0-" + _list_suffix("e-1"))
    await rig.gateway.poll()

    assert rig.gateway.entry_state("e-1") == "closed"
    assert rig.fake.uncovered_base() == 0
    assert rig.fake.free("BTC") == 0


def _list_suffix(entry_id: str) -> str:
    from engine.infrastructure.live.gateway import derived_id

    return derived_id(entry_id, "L0").removeprefix("L0-")


async def test_pending_activation_within_grace_is_not_rescued_but_after_grace_is(fake: FakeBinance) -> None:
    from tests.unit.live.support import build_rig

    fake.hold_activation = True
    rig = build_rig(fake, protection_activation_grace_seconds=3600)
    await _filled(rig)
    assert [r.path for r in fake.writes()] == ["/api/v3/orderList/opoco"]
    assert rig.gateway.protection_of("e-1") is None

    late = build_rig(fake, protection_activation_grace_seconds=0)
    late.gateway._entries = rig.gateway._entries
    await late.gateway.poll()
    assert late.gateway.protection_of("e-1") == (D(10), D(95), D(111))
    assert fake.uncovered_base() == 0
    await rig.client.aclose()
    await late.client.aclose()


async def test_partial_fill_is_truncated_and_protected_after_grace(fake: FakeBinance) -> None:
    from tests.unit.live.support import build_rig

    rig = build_rig(fake, partial_fill_grace_seconds=0)
    fake.fill_fraction = D("0.5")
    await _filled(rig)

    assert rig.gateway.protection_of("e-1") == (D(5), D(95), D(111))
    assert fake.uncovered_base() == 0
    assert fake.free("USDT") == D(10_000) - D(495)
    await rig.client.aclose()


async def test_partial_fill_inside_grace_waits(fake: FakeBinance) -> None:
    from tests.unit.live.support import build_rig

    rig = build_rig(fake, partial_fill_grace_seconds=3600)
    fake.fill_fraction = D("0.5")
    await _filled(rig)

    assert rig.gateway.protection_of("e-1") is None
    assert len(fake.writes()) == 1
    await rig.client.aclose()


async def test_emergency_failure_halts_gateway_and_raises(rig: Rig) -> None:
    from engine.infrastructure.live.errors import ProtectionError, TradingHaltedError

    rig.fake.fail_activation = True
    rig.fake.fail("/api/v3/orderList/oco", times=9, kind="binance", status=400, code=-1000)
    rig.fake.fail("/api/v3/order", method="POST", times=9, kind="binance", status=400, code=-1000)
    await rig.gateway.place_order(approved_entry())
    rig.fake.set_price("BTCUSDT", D(99))

    with pytest.raises(ProtectionError):
        await rig.gateway.poll()

    assert rig.gateway.halted
    with pytest.raises(TradingHaltedError):
        await rig.gateway.place_order(approved_entry(cid="e-2"))
