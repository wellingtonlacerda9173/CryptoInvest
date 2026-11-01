# ruff: noqa: E501
"""Rodada 10: reprotecao parcial x recuperacao, residuo proprio, ids de trade por simbolo e cursor do reconcile.
Somente a fake local."""

import contextlib
from dataclasses import replace
from decimal import Decimal
from typing import Any

import pytest

from engine.infrastructure.live import gateway as gateway_module
from engine.infrastructure.live.errors import LiveError
from engine.infrastructure.live.gateway import ProtectionIntent
from tests.fakes.fake_binance import FakeBinance
from tests.unit.live.support import Rig
from tests.unit.live.test_exits_reconcile import local_position
from tests.unit.live.test_recover import confirmation, covered, persisted, protected_then_down, restart
from tests.unit.live.test_review_round6 import extra_trade
from tests.unit.live.test_review_round7 import manual_sale
from tests.unit.live.test_review_round9 import DESPROTEGIDA, drop_live_protection, sale_ids

D = Decimal


def desprotegidas(rig: Rig) -> list[tuple[str, str]]:
    return [a for a in rig.alerts if a[0] == "critical" and a[1].startswith(DESPROTEGIDA)]


async def polls(rig: Rig, times: int = 3) -> None:
    for _ in range(times):
        with contextlib.suppress(LiveError):
            await rig.gateway.poll()


async def degraded(fake: FakeBinance, sold: str = "3", **overrides: Any) -> Rig:
    """Protecao do robo some, venda sem dono na conta, reinicio: recuperacao em modo degradado."""
    fake.balances["BTC"] = [D(5), D(0)]
    first = await protected_then_down(fake)
    drop_live_protection(fake)
    await manual_sale(fake, sold)
    second = await restart(first, **overrides)
    await second.gateway.recover([persisted()])
    return second


async def _partial_then_restart(fake: FakeBinance, *, stale_generation: bool, ack: bool) -> Rig:
    intents: list[ProtectionIntent] = []
    fake.balances["BTC"] = [D(5), D(0)]
    first = await protected_then_down(fake)
    drop_live_protection(fake)
    await manual_sale(fake, "3")
    second = await restart(first, on_intent=intents.append)
    await second.gateway.recover([persisted()])
    await second.gateway.poll()
    assert covered(fake) == D(7)
    entry = persisted() if stale_generation else intents[-1].persisted(persisted())
    entry = replace(entry, acknowledged_trade_ids=sale_ids(fake) if ack else (), acknowledged_by="ana")
    third = await restart(second)
    await third.gateway.recover([entry])
    await third.gateway.poll()
    return third


# ---- achado 1: OCO parcial dispara com residuo proprio -----------------------------------------------------------


async def test_partial_oco_firing_does_not_close_the_entry_with_an_own_residual(fake: FakeBinance) -> None:
    second = await degraded(fake)
    try:
        await second.gateway.poll()
        assert covered(fake) == D(7)
        second.alerts.clear()
        fake.set_price("BTCUSDT", D(90))
        await polls(second)

        assert second.gateway.entry_state("e-1") != "closed"
        assert second.gateway.halted
        assert len([a for a in second.alerts if a[0] == "critical" and "residuo 3" in a[1]]) == 1
        assert fake.free("BTC") + fake.locked("BTC") == D(5)
    finally:
        await second.client.aclose()


# ---- achado 2: geracao anunciada tambem no _arm ------------------------------------------------------------------


async def test_every_new_generation_is_announced_even_when_armed_outside_a_replace(fake: FakeBinance) -> None:
    intents: list[ProtectionIntent] = []
    fake.balances["BTC"] = [D(5), D(0)]
    first = await protected_then_down(fake)
    drop_live_protection(fake)
    await manual_sale(fake, "3")
    second = await restart(first, on_intent=intents.append)
    try:
        await second.gateway.recover([persisted()])
        await second.gateway.poll()

        assert covered(fake) == D(7)
        assert [i.generation for i in intents] == [4]
    finally:
        await second.client.aclose()


async def test_ack_after_partial_reprotection_completes_the_cover_without_double_cover(
    fake: FakeBinance,
) -> None:
    third = await _partial_then_restart(fake, stale_generation=False, ack=True)
    try:
        assert covered(fake) == D(10)
        assert fake.free("BTC") + fake.locked("BTC") == D(12)
        assert third.gateway.entry_state("e-1") == "protected"
    finally:
        await third.client.aclose()


async def test_a_failing_intent_sink_does_not_prevent_the_partial_protection(fake: FakeBinance) -> None:
    def broken(_: ProtectionIntent) -> None:
        raise OSError("disco cheio")

    second = await degraded(fake, on_intent=broken)
    try:
        await second.gateway.poll()

        assert covered(fake) == D(7)
    finally:
        await second.client.aclose()


# ---- achado 3: ids de trade sao por simbolo ----------------------------------------------------------------------


async def test_an_acknowledged_trade_id_never_hides_the_same_id_in_another_symbol(fake: FakeBinance) -> None:
    fake.symbols = ("BTCUSDT", "ETHUSDT")
    both = {"allowed_symbols": frozenset({"BTC/USDT", "ETH/USDT"})}
    fake.balances["BTC"] = [D(5), D(0)]
    first = await protected_then_down(fake)
    await manual_sale(fake, "3")
    ids = sale_ids(fake)
    second = await restart(first, **both)
    try:
        await second.gateway.recover(
            [replace(persisted(), acknowledged_trade_ids=ids, acknowledged_by="ana")]
        )
        fake.trades.append(
            extra_trade(ids[0], "1", buyer=False, at_ms=fake.trades[-1]["time"] + 1) | {"symbol": "ETHUSDT"}
        )

        result = await second.gateway.reconcile([local_position()])

        assert f"ETH/USDT:{ids[0]}" in result.unknown_trade_refs
        assert not [r for r in result.unknown_trade_refs if r.startswith("BTC/USDT")]
        assert "ETH/USDT" in result.describe()
        assert second.gateway.halted
    finally:
        await second.client.aclose()


# ---- baixa 4: cursor e conjunto de desconhecidos --------------------------------------------------------------------


async def test_restart_zeroes_the_cursor_but_the_unknown_trades_are_read_again(fake: FakeBinance) -> None:
    first = await protected_then_down(fake)
    base = fake.trades[-1]["time"]
    fake.trades.append(extra_trade(10_000, "0.001", buyer=True, at_ms=base + 1))
    try:
        assert 10_000 in (await first.gateway.reconcile([local_position()])).unknown_trades
        assert first.gateway._trade_cursor
    finally:
        await first.client.aclose()
    second = await restart(first)
    try:
        assert second.gateway._trade_cursor == {}

        assert 10_000 in (await second.gateway.reconcile([local_position()])).unknown_trades
    finally:
        await second.client.aclose()


async def test_the_unknown_trade_set_is_bounded_and_keeps_the_newest(
    fake: FakeBinance, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gateway_module, "_MAX_UNKNOWN_TRADES", 5)
    rig = await protected_then_down(fake)
    try:
        base = fake.trades[-1]["time"]
        for i in range(8):
            fake.trades.append(extra_trade(10_000 + i, "0.001", buyer=True, at_ms=base + 1 + i))

        result = await rig.gateway.reconcile([local_position()])

        assert result.unknown_trades == tuple(range(10_003, 10_008))
        assert len(rig.gateway._unknown_trades["BTC/USDT"]) == 5
    finally:
        await rig.client.aclose()


# ---- baixa 5: lacunas de teste ------------------------------------------------------------------------------------


async def test_partial_protection_is_capped_by_the_free_balance_below_owed(fake: FakeBinance) -> None:
    second = await degraded(fake)
    try:
        fake.balances["BTC"] = [D(4), D(0)]
        await second.gateway.poll()

        assert covered(fake) == D(4)
    finally:
        await second.client.aclose()


@pytest.mark.parametrize(
    ("min_qty", "sold"), [("0.01", "9.995"), ("0.001", "9.999")], ids=["below-min-qty", "below-min-notional"]
)
async def test_partial_parts_below_the_exchange_minimums_stay_exposed_with_an_alert_and_arm_nothing(
    fake: FakeBinance, min_qty: str, sold: str
) -> None:
    fake.min_qty = D(min_qty)
    second = await degraded(fake, sold=sold)
    try:
        await polls(second, 1)
        alerted = len(desprotegidas(second))
        await polls(second, 4)

        assert alerted >= 1
        assert len(desprotegidas(second)) == alerted
        assert covered(fake) == 0
        assert second.gateway.entry_state("e-1") != "closed"
        assert second.gateway.halted
        assert fake.free("BTC") + fake.locked("BTC") == D(5) + D(10) - D(sold)
    finally:
        await second.client.aclose()


async def test_the_critical_human_alert_is_sent_once_per_entry(fake: FakeBinance) -> None:
    second = await degraded(fake, sold="10")
    try:
        await polls(second, 5)

        assert len(desprotegidas(second)) == 1
    finally:
        await second.client.aclose()


async def test_emergency_sells_only_what_the_entry_owes_with_a_pending_sale(fake: FakeBinance) -> None:
    second = await degraded(fake)
    try:
        fake.fail("/api/v3/orderList/oco", method="POST", times=500, kind="binance", status=400, code=-1000)
        await polls(second, 2)

        assert fake.free("BTC") + fake.locked("BTC") == D(5)
        assert second.gateway.entry_state("e-1") != "closed"
    finally:
        await second.client.aclose()


async def test_raise_stop_with_a_pending_sale_rearms_only_what_the_entry_owes(
    fake: FakeBinance,
) -> None:
    second = await degraded(fake)
    try:
        await second.gateway.poll()
        assert covered(fake) == D(7)

        await second.gateway.raise_stop("e-1", D(97))

        assert covered(fake) == D(7)
        assert fake.free("BTC") + fake.locked("BTC") == D(12)
    finally:
        await second.client.aclose()


async def test_close_orphan_with_a_pending_sale_never_sells_above_owed(fake: FakeBinance) -> None:
    second = await degraded(fake)
    try:
        result = await second.gateway.close_orphan("BTC/USDT", D(10), confirmation("10"))

        assert result.executed == D(7)
        assert fake.free("BTC") + fake.locked("BTC") == D(5)
        assert second.gateway.entry_state("e-1") == "closed"
    finally:
        await second.client.aclose()
