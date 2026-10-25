"""Reinicio no meio de cada operacao: `recover` reconstroi o estado so com ids deterministicos, e
`close_orphan` e o caminho manual limitado e auditado. Somente a fake local."""

from decimal import Decimal
from typing import Any

import pytest

from engine.infrastructure.live.errors import FilterViolationError
from engine.infrastructure.live.gateway import derived_id
from engine.infrastructure.live.recovery import OperatorConfirmation, PersistedEntry
from tests.fakes.fake_binance import FakeBinance
from tests.unit.live.support import SYMBOL, Rig, approved_entry, build_rig
from tests.unit.live.test_exits_reconcile import local_position
from tests.unit.live.test_review_round4 import settle

D = Decimal


def persisted(cid: str = "e-1", generation: int = 0, exits: tuple[str, ...] = ()) -> PersistedEntry:
    approved = approved_entry(cid=cid)
    return PersistedEntry(
        order=approved.order,
        expires_at=approved.guards.expires_at,
        generation=generation,
        exit_client_ids=exits,
    )


def covered(fake: FakeBinance) -> Decimal:
    return sum((o.qty - o.executed for o in fake.active_stops()), D(0))


def confirmation(quantity: str, operator: str = "ana") -> OperatorConfirmation:
    return OperatorConfirmation(operator=operator, confirmed_quantity=D(quantity), reason="posicao orfa")


async def restart(first: Rig, **overrides: Any) -> Rig:
    await first.client.aclose()
    return build_rig(first.fake, **overrides)


async def expose_with_failures(fake: FakeBinance, rig: Rig) -> None:
    fake.fail_activation = True
    fake.fail("/api/v3/orderList/oco", method="POST", times=500, kind="binance", status=400, code=-1000)
    fake.fail("/api/v3/order", method="POST", times=500, kind="binance", status=400, code=-1000)
    await rig.gateway.place_order(approved_entry())
    fake.set_price("BTCUSDT", D(99))
    await settle(rig.gateway.poll)
    assert rig.gateway.entry_state("e-1") == "exposed"


def sell_outside_the_gateway(fake: FakeBinance, quantity: str, cid: str = "x-1") -> None:
    fake._delete_list({"symbol": "BTCUSDT", "listClientOrderId": derived_id("e-1", "L0")})
    fake._post_order(
        {"symbol": "BTCUSDT", "type": "MARKET", "side": "SELL", "quantity": quantity, "newClientOrderId": cid}
    )


async def protected_then_down(fake: FakeBinance) -> Rig:
    first = build_rig(fake)
    await first.gateway.place_order(approved_entry())
    fake.set_price("BTCUSDT", D(99))
    await first.gateway.poll()
    fake.set_price("BTCUSDT", D(103))
    return first


async def test_restart_with_a_resting_buy_recovers_working_and_protects_after_the_fill(
    fake: FakeBinance,
) -> None:
    first = build_rig(fake)
    await first.gateway.place_order(approved_entry())
    second = await restart(first)
    try:
        result = await second.gateway.recover([persisted()])
        fake.set_price("BTCUSDT", D(99))
        await second.gateway.poll()

        assert [r.outcome for r in result.entries] == ["working"]
        assert second.gateway.protection_of("e-1") == (D(10), D(95), D(111))
        assert fake.uncovered_base() == 0
    finally:
        await second.client.aclose()


async def test_restart_with_a_protected_position_adopts_the_live_protection_without_writing(
    fake: FakeBinance,
) -> None:
    second = await restart(await protected_then_down(fake))
    try:
        fake.reset_log()
        result = await second.gateway.recover([persisted()])

        assert [r.outcome for r in result.entries] == ["protected"]
        assert fake.writes() == []
        assert second.gateway.protection_of("e-1") == (D(10), D(95), D(111))
        reconciled = await second.gateway.reconcile([local_position()])
        assert reconciled.ok, reconciled.describe()
        assert not second.gateway.halted
        fake.set_price("BTCUSDT", D(112))
        await second.gateway.poll()
        assert second.gateway.entry_state("e-1") == "closed"
        assert fake.free("BTC") == 0
    finally:
        await second.client.aclose()


async def test_restart_after_the_target_filled_closes_flat_and_never_touches_foreign_balance(
    fake: FakeBinance,
) -> None:
    fake.balances["BTC"] = [D(5), D(0)]
    first = await protected_then_down(fake)
    fake.set_price("BTCUSDT", D(112))
    second = await restart(first)
    try:
        fake.reset_log()
        result = await second.gateway.recover([persisted()])

        assert [r.outcome for r in result.entries] == ["flat"]
        assert second.gateway.entry_state("e-1") == "closed"
        assert fake.writes() == []
        assert fake.free("BTC") == 5
        assert not [a for a in second.alerts if a[0] == "critical"]
    finally:
        await second.client.aclose()


async def test_restart_with_a_partial_fill_adopts_the_standalone_stop_and_covers_the_delta(
    fake: FakeBinance,
) -> None:
    first = build_rig(fake, partial_fill_grace_seconds=0)
    await first.gateway.place_order(approved_entry())
    fake.fill_fraction = D("0.5")
    fake.fail("/api/v3/orderList", method="DELETE", times=1000, kind="binance", status=400, code=-1000)
    fake.set_price("BTCUSDT", D(99))
    await settle(first.gateway.poll)
    assert covered(fake) == D(5)
    second = await restart(first, partial_fill_grace_seconds=0)
    try:
        result = await second.gateway.recover([persisted()])
        assert [r.outcome for r in result.entries] == ["protected"]
        assert covered(fake) == D(5)

        fake.faults.clear()
        fake.set_price("BTCUSDT", D(98))
        await second.gateway.poll()

        assert covered(fake) == D(10)
        assert fake.uncovered_base() == 0
        assert second.gateway.entry_state("e-1") == "protected"
    finally:
        await second.client.aclose()


async def test_restart_with_an_exposed_position_reprotects_only_what_the_entry_owns(
    fake: FakeBinance,
) -> None:
    fake.balances["BTC"] = [D(5), D(0)]
    first = build_rig(fake, protection_retries=1)
    await expose_with_failures(fake, first)
    fake.faults.clear()
    fake.fail_activation = False
    second = await restart(first)
    try:
        result = await second.gateway.recover([persisted()])

        assert [r.outcome for r in result.entries] == ["reprotected"]
        assert covered(fake) == D(10)
        assert fake.free("BTC") == D(5)
    finally:
        await second.client.aclose()


async def test_restart_during_an_exit_deducts_the_persisted_exit_and_protects_the_rest(
    fake: FakeBinance,
) -> None:
    fake.balances["BTC"] = [D(5), D(0)]
    first = await protected_then_down(fake)
    sell_outside_the_gateway(fake, "4")
    second = await restart(first)
    try:
        result = await second.gateway.recover([persisted(exits=("x-1",))])

        assert [r.outcome for r in result.entries] == ["reprotected"]
        assert covered(fake) == D(6)
        assert fake.free("BTC") == D(5)
    finally:
        await second.client.aclose()


async def test_restart_with_an_unexplained_sale_refuses_to_protect_or_sell_and_pauses(
    fake: FakeBinance,
) -> None:
    fake.balances["BTC"] = [D(5), D(0)]
    first = await protected_then_down(fake)
    sell_outside_the_gateway(fake, "4")
    second = await restart(first)
    try:
        fake.reset_log()
        result = await second.gateway.recover([persisted()])

        assert [r.outcome for r in result.entries] == ["exposed"]
        assert second.gateway.halted
        assert second.gateway.entry_state("e-1") == "exposed"
        assert fake.writes() == []
        assert fake.free("BTC") == D(11)
        assert any(level == "critical" for level, _ in second.alerts)
    finally:
        await second.client.aclose()


async def test_unplaced_entry_is_not_recovered(fake: FakeBinance) -> None:
    rig = build_rig(fake)
    try:
        result = await rig.gateway.recover([persisted()])

        assert [r.outcome for r in result.entries] == ["not_placed"]
        assert rig.gateway.open_entry_ids() == []
    finally:
        await rig.client.aclose()


async def test_recover_is_idempotent_for_known_entries(fake: FakeBinance) -> None:
    first = await protected_then_down(fake)
    try:
        result = await first.gateway.recover([persisted()])

        assert [r.outcome for r in result.entries] == ["already_known"]
    finally:
        await first.client.aclose()


async def orphan_rig(fake: FakeBinance, audits: list[dict[str, Any]]) -> Rig:
    first = build_rig(fake, protection_retries=1)
    await expose_with_failures(fake, first)
    second = await restart(first, protection_retries=1)
    second.gateway._audit_cb = audits.append
    result = await second.gateway.recover([persisted()])
    assert [r.outcome for r in result.entries] == ["exposed"]
    fake.faults.clear()
    fake.fail_activation = False
    return second


async def test_close_orphan_is_limited_to_the_reconstructed_position_and_audited(
    fake: FakeBinance,
) -> None:
    fake.balances["BTC"] = [D(5), D(0)]
    audits: list[dict[str, Any]] = []
    rig = await orphan_rig(fake, audits)
    try:
        result = await rig.gateway.close_orphan(SYMBOL, D(100), confirmation("100"))

        assert result.applied == D(10) and result.executed == D(10)
        assert fake.free("BTC") == D(5), "close_orphan vendeu saldo alheio"
        assert [a["event"] for a in audits] == ["orphan_close_intent", "orphan_close_done"]
        assert audits[0]["operator"] == "ana" and D(audits[0]["applied"]) == D(10)
        assert rig.gateway.entry_state("e-1") == "closed"
    finally:
        await rig.client.aclose()


async def test_close_orphan_is_limited_by_the_free_balance(fake: FakeBinance) -> None:
    rig = await orphan_rig(fake, [])
    try:
        fake.balances["BTC"][0] -= D(3)

        first = await rig.gateway.close_orphan(SYMBOL, D(4), confirmation("4"))
        assert first.applied == D(4) and fake.free("BTC") == D(3)
        second = await rig.gateway.close_orphan(SYMBOL, D(50), confirmation("50"))
        assert second.applied == D(3) and fake.free("BTC") == 0
    finally:
        await rig.client.aclose()


@pytest.mark.parametrize(
    ("quantity", "confirmed", "operator"),
    [("5", "6", "ana"), ("5", "5", "  "), ("0", "0", "ana")],
)
async def test_close_orphan_requires_explicit_matching_confirmation(
    fake: FakeBinance, quantity: str, confirmed: str, operator: str
) -> None:
    audits: list[dict[str, Any]] = []
    rig = await orphan_rig(fake, audits)
    try:
        fake.reset_log()
        with pytest.raises(FilterViolationError):
            await rig.gateway.close_orphan(SYMBOL, D(quantity), confirmation(confirmed, operator))

        assert fake.writes() == [] and audits == []
    finally:
        await rig.client.aclose()


async def test_close_orphan_never_sells_a_position_that_has_a_live_protection(fake: FakeBinance) -> None:
    second = await restart(await protected_then_down(fake))
    try:
        fake.fail("/api/v3/order", method="GET", times=1000, kind="binance", status=400, code=-1000)
        result = await second.gateway.recover([persisted()])
        assert [r.outcome for r in result.entries] == ["exposed"]
        fake.faults.clear()
        fake.reset_log()

        with pytest.raises(FilterViolationError):
            await second.gateway.close_orphan(SYMBOL, D(10), confirmation("10"))

        assert fake.writes() == []
        assert second.gateway.entry_state("e-1") == "protected"
        assert covered(fake) == D(10)
    finally:
        await second.client.aclose()
