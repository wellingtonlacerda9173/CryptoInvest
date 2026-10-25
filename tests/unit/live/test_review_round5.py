"""Correcoes da 4a rodada: compra parcial com cancel nao confirmado e debounce do alerta de emergencia."""

from decimal import Decimal

from tests.fakes.fake_binance import FakeBinance
from tests.unit.live.support import approved_entry, build_rig
from tests.unit.live.test_review_round4 import settle

D = Decimal


def covered(fake: FakeBinance) -> Decimal:
    return sum((o.qty - o.executed for o in fake.active_stops()), D(0))


async def test_partial_fill_with_unconfirmed_cancel_is_always_protected_and_the_delta_is_covered(
    fake: FakeBinance,
) -> None:
    rig = build_rig(fake, partial_fill_grace_seconds=0)
    try:
        gateway = rig.gateway
        await gateway.place_order(approved_entry())
        fake.fill_fraction = D("0.5")
        fake.fail("/api/v3/orderList", method="DELETE", times=1000, kind="binance", status=400, code=-1000)
        fake.set_price("BTCUSDT", D(99))

        for _ in range(4):
            await settle(gateway.poll)
            assert fake.uncovered_base() == 0, "executado parcial ficou sem stop"
            assert covered(fake) == D(5)
            assert gateway.entry_state("e-1") == "protected"
            assert gateway.halted

        fake.faults.clear()
        fake.set_price("BTCUSDT", D(98))
        assert fake.uncovered_base() == D(5)
        await settle(gateway.poll)

        assert fake.uncovered_base() == 0
        assert covered(fake) == D(10)
        assert gateway.entry_state("e-1") == "protected"
    finally:
        await rig.client.aclose()


async def test_partial_fill_with_unconfirmed_cancel_does_not_sell_foreign_balance_when_flat(
    fake: FakeBinance,
) -> None:
    fake.balances["BTC"] = [D(3), D(0)]
    rig = build_rig(fake, partial_fill_grace_seconds=0)
    try:
        await rig.gateway.place_order(approved_entry())
        fake.fill_fraction = D("0.5")
        fake.fail("/api/v3/orderList", method="DELETE", times=1000, kind="binance", status=400, code=-1000)
        fake.set_price("BTCUSDT", D(99))
        await settle(rig.gateway.poll)
        fake.set_price("BTCUSDT", D(112))
        fake.faults.clear()

        for _ in range(3):
            await settle(rig.gateway.poll)

        assert fake.free("BTC") + fake.locked("BTC") == D(3)
    finally:
        await rig.client.aclose()


async def _failing_emergency(fake: FakeBinance, **overrides: float) -> list[tuple[str, str]]:
    fake.fail_activation = True
    rig = build_rig(fake, protection_retries=1, emergency_max_attempts=1, **overrides)
    try:
        await rig.gateway.place_order(approved_entry())
        fake.fail("/api/v3/orderList/oco", method="POST", times=500, kind="binance", status=400, code=-1000)
        fake.fail("/api/v3/order", method="POST", times=500, kind="binance", status=400, code=-1000)
        fake.set_price("BTCUSDT", D(99))
        for _ in range(4):
            await settle(rig.gateway.poll)
        return [a for a in rig.alerts if "venda de emergencia (" in a[1]]
    finally:
        await rig.client.aclose()


async def test_emergency_alert_is_sent_once_per_episode(fake: FakeBinance) -> None:
    alerts = await _failing_emergency(fake, emergency_realert_seconds=3600)

    assert len(alerts) == 1


async def test_emergency_alert_is_reescalated_after_the_interval(fake: FakeBinance) -> None:
    alerts = await _failing_emergency(fake, emergency_realert_seconds=1e-9)

    assert len(alerts) >= 3
