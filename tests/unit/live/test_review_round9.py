# ruff: noqa: E501
"""Rodada 9: reprotecao parcial sem protecao viva, confirmacao por ids de trade e cursor do reconcile.
Somente a fake local (nada de Binance real)."""

import contextlib
from dataclasses import replace
from decimal import Decimal

import pytest

from engine.infrastructure.live import gateway as gateway_module
from engine.infrastructure.live.errors import LiveError
from tests.fakes.fake_binance import FakeBinance
from tests.unit.live.test_exits_reconcile import local_position
from tests.unit.live.test_recover import covered, persisted, protected_then_down, restart
from tests.unit.live.test_review_round6 import extra_trade, paginate_my_trades
from tests.unit.live.test_review_round7 import manual_sale
from tests.unit.live.test_review_round8 import paged_requests

D = Decimal
DESPROTEGIDA = "POSICAO PROPRIA DESPROTEGIDA: acao humana necessaria"


def sale_ids(fake: FakeBinance) -> tuple[int, ...]:
    return tuple(int(t["id"]) for t in fake.trades if not t["isBuyer"])


def drop_live_protection(fake: FakeBinance) -> None:
    for list_id in [item.cid for item in fake.lists.values() if item.status == "EXECUTING"]:
        fake._delete_list({"symbol": "BTCUSDT", "listClientOrderId": list_id})


# ---- M2: sem protecao viva + venda sem dono -------------------------------------------------------------------


async def test_without_live_protection_the_poll_protects_only_the_probably_own_part_and_alerts(
    fake: FakeBinance,
) -> None:
    fake.balances["BTC"] = [D(5), D(0)]
    first = await protected_then_down(fake)
    drop_live_protection(fake)
    await manual_sale(fake, "3")
    second = await restart(first)
    try:
        result = await second.gateway.recover([persisted()])
        assert [r.outcome for r in result.entries] == ["exposed"], result
        assert DESPROTEGIDA in result.entries[0].detail
        assert covered(fake) == 0

        await second.gateway.poll()

        assert covered(fake) == D(7)
        assert covered(fake) <= D(10) - D(3)
        assert second.gateway.entry_state("e-1") == "protected"
        assert second.gateway.halted
        assert fake.free("BTC") + fake.locked("BTC") == D(12)
        assert [a for a in second.alerts if a[0] == "critical" and DESPROTEGIDA in a[1]]

        await second.gateway.poll()
        assert covered(fake) == D(7)
    finally:
        await second.client.aclose()


async def test_when_the_probable_part_is_zero_the_current_behaviour_is_kept(fake: FakeBinance) -> None:
    fake.balances["BTC"] = [D(5), D(0)]
    first = await protected_then_down(fake)
    drop_live_protection(fake)
    await manual_sale(fake, "10")
    second = await restart(first)
    try:
        await second.gateway.recover([persisted()])
        with contextlib.suppress(LiveError):
            await second.gateway.poll()

        assert covered(fake) == 0
        assert fake.free("BTC") + fake.locked("BTC") == D(5)
        assert second.gateway.halted
    finally:
        await second.client.aclose()


async def test_live_oco_firing_while_exposed_ends_safely_without_loops_or_false_positives(
    fake: FakeBinance,
) -> None:
    fake.balances["BTC"] = [D(5), D(0)]
    first = await protected_then_down(fake)
    await manual_sale(fake, "3")
    second = await restart(first)
    try:
        result = await second.gateway.recover([persisted()])
        assert [r.outcome for r in result.entries] == ["exposed"], result
        assert covered(fake) == D(10)

        fake.set_price("BTCUSDT", D(90))
        for _ in range(3):
            await second.gateway.poll()

        assert second.gateway.entry_state("e-1") == "closed"
        assert covered(fake) == 0
        assert fake.free("BTC") + fake.locked("BTC") == D(2)
        reconciled = await second.gateway.reconcile([])
        assert set(reconciled.unknown_trades) == set(sale_ids(fake)[:1])
        assert reconciled.protection_mismatches == ()
        assert reconciled.unknown_open_orders == ()
    finally:
        await second.client.aclose()


# ---- M3: confirmacao por ids de trade -------------------------------------------------------------------------


async def test_an_old_acknowledgement_never_covers_a_new_foreign_trade(fake: FakeBinance) -> None:
    fake.balances["BTC"] = [D(5), D(0)]
    first = await protected_then_down(fake)
    await manual_sale(fake, "3")
    old = sale_ids(fake)
    second = await restart(first)
    confirmed = replace(persisted(), acknowledged_trade_ids=old, acknowledged_by="ana")
    try:
        result = await second.gateway.recover([confirmed])
        assert [r.outcome for r in result.entries] == ["protected"], result
        assert [a for a in second.alerts if a[0] == "warning" and "ana" in a[1]]
        assert (await second.gateway.reconcile([local_position()])).unknown_trades == ()
    finally:
        await second.client.aclose()
    fake._post_order(
        {
            "symbol": "BTCUSDT",
            "type": "MARKET",
            "side": "SELL",
            "quantity": "2",
            "newClientOrderId": "manual-2",
        }
    )
    third = await restart(second)
    try:
        result = await third.gateway.recover([confirmed])

        assert [r.outcome for r in result.entries] == ["exposed"], result
        assert third.gateway.halted
        assert covered(fake) == D(10)
        assert str(sale_ids(fake)[-1]) in result.entries[0].detail
    finally:
        await third.client.aclose()


async def test_the_acknowledged_quantity_alone_no_longer_exists_as_a_ceiling(fake: FakeBinance) -> None:
    assert not hasattr(persisted(), "acknowledged_foreign_sales")


# ---- M4: cursor fromId no reconcile ---------------------------------------------------------------------------


async def test_reconcile_advances_by_cursor_and_recovers_from_a_long_history(
    fake: FakeBinance, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = await protected_then_down(fake)
    try:
        paginate_my_trades(fake, 1000)
        base = fake.trades[-1]["time"]
        for i in range(1100):
            fake.trades.append(extra_trade(10_000 + i, "0.001", buyer=True, at_ms=base + 1 + i))
        monkeypatch.setattr(gateway_module, "_MAX_TRADE_PAGES", 1)

        first = await rig.gateway.reconcile([local_position()])
        assert first.inconclusive == ("BTC/USDT",)

        fake.reset_log()
        results = [await rig.gateway.reconcile([local_position()])]
        assert "fromId" in paged_requests(fake)[0].params
        assert "startTime" not in paged_requests(fake)[0].params
        results.append(await rig.gateway.reconcile([local_position()]))

        assert results[-1].inconclusive == ()
        assert 11_099 in results[-1].unknown_trades
        assert not results[-1].ok
    finally:
        await rig.client.aclose()
