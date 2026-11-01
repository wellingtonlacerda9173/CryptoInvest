# ruff: noqa: E501
"""Fechamento da etapa 1 do QA: perna de stop parcial, geracoes antigas no recover, trades paginados e
venda alheia com protecao viva. Somente a fake local (nenhuma chamada a Binance real/testnet)."""

from decimal import Decimal
from typing import Any

import pytest

from engine.domain.gateway import UnknownOrderError
from engine.infrastructure.live.gateway import derived_id
from tests.fakes.fake_binance import FakeBinance
from tests.unit.live.support import SYMBOL, Rig, approved_entry, build_rig
from tests.unit.live.test_exits_reconcile import local_position
from tests.unit.live.test_recover import covered, persisted, protected_then_down, restart
from tests.unit.live.test_review_round4 import settle
from tests.unit.live.test_review_round6 import (
    extra_trade,
    market_exit,
    paginate_my_trades,
    partially_filled_stop_leg,
    reuse_closed_ids,
)

D = Decimal
FOREIGN = D(5)


def assert_foreign_intact(fake: FakeBinance) -> None:
    assert fake.free("BTC") + fake.locked("BTC") >= FOREIGN
    bought = sum((D(t["qty"]) for t in fake.trades if t["isBuyer"]), D(0))
    sold = sum((D(t["qty"]) for t in fake.trades if not t["isBuyer"]), D(0))
    assert sold <= bought, f"vendeu {sold} de {bought}"


async def partial_stop(fake: FakeBinance, rig: Rig, executed: str) -> None:
    await rig.gateway.place_order(approved_entry())
    fake.set_price("BTCUSDT", D(99))
    stop_leg = next(o for o in fake.orders.values() if o.cid == derived_id("e-1", "S0"))
    fake._fill(stop_leg, D(executed), D(94), maker=False)
    assert stop_leg.status == "PARTIALLY_FILLED"


# ---- perna de stop PARTIALLY_FILLED: viva, adotada com o restante, ledger abatido ---------------------------


async def test_poll_adopts_the_partially_filled_stop_leg_with_its_remainder_and_writes_nothing(
    fake: FakeBinance,
) -> None:
    fake.balances["BTC"] = [FOREIGN, D(0)]
    rig = build_rig(fake)
    try:
        await partially_filled_stop_leg(fake, rig)
        fake.reset_log()

        await settle(rig.gateway.poll)

        assert rig.gateway.protection_of("e-1") == (D(6), D(95), D(111))
        assert fake.order_writes() == []
        assert covered(fake) == D(6)
        assert not rig.gateway.halted
        assert not [a for a in rig.alerts if a[0] == "critical"]
    finally:
        await rig.client.aclose()


async def test_partially_filled_stop_leg_that_finishes_closes_flat_without_touching_foreign_balance(
    fake: FakeBinance,
) -> None:
    fake.balances["BTC"] = [FOREIGN, D(0)]
    rig = build_rig(fake)
    try:
        await partially_filled_stop_leg(fake, rig)
        await settle(rig.gateway.poll)
        fake.set_price("BTCUSDT", D("94.8"))
        await settle(rig.gateway.poll)

        assert rig.gateway.entry_state("e-1") == "closed"
        assert fake.free("BTC") == FOREIGN
        assert fake.locked("BTC") == 0
    finally:
        await rig.client.aclose()


async def test_raise_stop_during_a_partially_filled_stop_leg_covers_only_the_remainder(
    fake: FakeBinance,
) -> None:
    fake.balances["BTC"] = [FOREIGN, D(0)]
    rig = build_rig(fake)
    try:
        await partially_filled_stop_leg(fake, rig)
        await settle(rig.gateway.poll)
        fake.set_price("BTCUSDT", D(103))

        await settle(lambda: rig.gateway.raise_stop("e-1", D(98)))

        assert rig.gateway.protection_of("e-1") == (D(6), D(98), D(111))
        assert covered(fake) == D(6)
        assert fake.free("BTC") == FOREIGN
    finally:
        await rig.client.aclose()


async def test_cancel_of_the_working_entry_after_a_partially_filled_stop_leg_is_a_noop_for_protection(
    fake: FakeBinance,
) -> None:
    fake.balances["BTC"] = [FOREIGN, D(0)]
    rig = build_rig(fake)
    try:
        await partially_filled_stop_leg(fake, rig)
        await settle(rig.gateway.poll)
        fake.reset_log()

        await settle(lambda: rig.gateway.cancel_order(SYMBOL, "e-1"))

        assert [r for r in fake.writes() if r.method == "DELETE"] == []
        assert covered(fake) == D(6)
        with pytest.raises(UnknownOrderError):
            await rig.gateway.cancel_order(SYMBOL, "inexistente")
    finally:
        await rig.client.aclose()


async def test_a_read_timeout_on_the_partial_stop_leg_never_exposes_nor_replaces_the_protection(
    fake: FakeBinance,
) -> None:
    fake.balances["BTC"] = [FOREIGN, D(0)]
    rig = build_rig(fake, request_timeout_seconds=0.2, read_retries=1)
    try:
        await partially_filled_stop_leg(fake, rig)
        fake.fail("/api/v3/order", method="GET", times=3, status=503)
        fake.reset_log()

        await settle(rig.gateway.poll)
        fake.faults.clear()
        await settle(rig.gateway.poll)

        assert [r for r in fake.order_writes() if r.method == "POST"] == []
        assert fake.locked("BTC") <= D(6) and fake.free("BTC") >= FOREIGN
        assert rig.gateway.entry_state("e-1") == "protected"
    finally:
        await rig.client.aclose()


async def test_reconcile_accepts_a_partially_filled_stop_leg_as_cover_for_the_remainder(
    fake: FakeBinance,
) -> None:
    fake.balances["BTC"] = [FOREIGN, D(0)]
    rig = build_rig(fake)
    try:
        await partially_filled_stop_leg(fake, rig)
        await settle(rig.gateway.poll)

        result = await rig.gateway.reconcile([local_position(qty="6")])

        assert result.protection_mismatches == ()
    finally:
        await rig.client.aclose()


@pytest.mark.parametrize("executed", ["1", "4", "9"])
@pytest.mark.parametrize("operation", ["poll", "exit", "raise", "drop", "recover", "recover_exit"])
async def test_matrix_partially_filled_stop_leg_never_touches_foreign_balance(
    fake: FakeBinance, executed: str, operation: str
) -> None:
    fake.balances["BTC"] = [FOREIGN, D(0)]
    first = build_rig(fake)
    rig = first
    try:
        await partial_stop(fake, first, executed)
        remaining = D(10) - D(executed)
        if operation.startswith("recover"):
            rig = await restart(first)
            await settle(lambda: rig.gateway.recover([persisted()]))
        match operation:
            case "poll":
                await settle(first.gateway.poll)
            case "exit" | "recover_exit":
                await settle(lambda: rig.gateway.place_order(market_exit(str(remaining))))
            case "raise":
                fake.set_price("BTCUSDT", D(103))
                await settle(lambda: first.gateway.raise_stop("e-1", D(98)))
            case "drop":
                await settle(first.gateway.poll)
                await settle(lambda: first.gateway.drop_target("e-1"))
        for _ in range(2):
            await settle(rig.gateway.poll)

        assert_foreign_intact(fake)
        assert fake.free("BTC") + fake.locked("BTC") >= FOREIGN
        assert fake.locked("BTC") <= remaining
    finally:
        await rig.client.aclose()


# ---- recover com geracao persistida antiga (stale) -----------------------------------------------------------


@pytest.mark.parametrize("raises", [1, 4, 12, 25])
async def test_recover_with_a_stale_generation_adopts_the_latest_protection_for_any_distance(
    fake: FakeBinance, raises: int
) -> None:
    fake.balances["BTC"] = [FOREIGN, D(0)]
    reuse_closed_ids(fake)
    first = await protected_then_down(fake)
    final = D("95.1") + D("0.1") * raises
    for index in range(1, raises + 1):
        await first.gateway.raise_stop("e-1", D("95") + D("0.1") * index)
    second = await restart(first)
    try:
        fake.reset_log()
        result = await second.gateway.recover([persisted(generation=0)])

        assert [r.outcome for r in result.entries] == ["protected"], result
        assert fake.order_writes() == []
        assert [o.stop for o in fake.active_stops()] == [final - D("0.1")]
        assert fake.free("BTC") == FOREIGN
        assert second.gateway.protection_of("e-1") == (D(10), final - D("0.1"), D(111))
    finally:
        await second.client.aclose()


async def test_recover_new_protection_never_reuses_a_generation_id_that_exists(fake: FakeBinance) -> None:
    fake.balances["BTC"] = [FOREIGN, D(0)]
    first = await protected_then_down(fake)
    for stop in (96, 97, 98):
        await first.gateway.raise_stop("e-1", D(stop))
    second = await restart(first)
    try:
        await second.gateway.recover([persisted(generation=0)])
        fake.set_price("BTCUSDT", D(103))
        await second.gateway.raise_stop("e-1", D(99))

        assert [o.stop for o in fake.active_stops()] == [D(99)]
        assert fake.free("BTC") == FOREIGN
    finally:
        await second.client.aclose()


# ---- /myTrades paginado: reconcile ---------------------------------------------------------------------------


async def test_reconcile_sees_unknown_trades_beyond_the_first_mytrades_page(fake: FakeBinance) -> None:
    rig = await protected_then_down(fake)
    try:
        paginate_my_trades(fake, 1000)
        base = fake.trades[-1]["time"]
        for i in range(1100):
            fake.trades.append(extra_trade(10_000 + i, "0.001", buyer=True, at_ms=base + 1 + i))

        result = await rig.gateway.reconcile([local_position()])

        assert 11_099 in result.unknown_trades
        assert not result.ok and rig.gateway.halted
    finally:
        await rig.client.aclose()


# ---- venda alheia com a protecao do robo VIVA ------------------------------------------------------------------


async def manual_sale(fake: FakeBinance, quantity: str) -> None:
    fake._post_order(
        {
            "symbol": "BTCUSDT",
            "type": "MARKET",
            "side": "SELL",
            "quantity": quantity,
            "newClientOrderId": "manual-1",
        }
    )


async def test_foreign_sale_with_live_protection_pauses_alerts_and_keeps_the_protection_untouched(
    fake: FakeBinance,
) -> None:
    fake.balances["BTC"] = [FOREIGN, D(0)]
    first = await protected_then_down(fake)
    await manual_sale(fake, "3")
    second = await restart(first)
    try:
        fake.reset_log()
        result = await second.gateway.recover([persisted()])

        assert [r.outcome for r in result.entries] == ["exposed"], result
        assert fake.order_writes() == []
        assert second.gateway.halted
        assert [a for a in second.alerts if a[0] == "critical"]
        assert covered(fake) == D(10)
        assert fake.free("BTC") == FOREIGN - D(3)
    finally:
        await second.client.aclose()


async def test_foreign_sale_without_live_protection_still_leaves_the_entry_exposed(fake: FakeBinance) -> None:
    fake.balances["BTC"] = [FOREIGN, D(0)]
    first = await protected_then_down(fake)
    fake._delete_list({"symbol": "BTCUSDT", "listClientOrderId": derived_id("e-1", "L0")})
    await manual_sale(fake, "3")
    second = await restart(first)
    try:
        result = await second.gateway.recover([persisted()])

        assert [r.outcome for r in result.entries] == ["exposed"], result
        assert second.gateway.halted
        assert fake.free("BTC") == FOREIGN + D(10) - D(3)
    finally:
        await second.client.aclose()


# ---- write-ahead da intencao de protecao ------------------------------------------------------------------------


async def test_every_replace_announces_its_intent_before_any_cancel(fake: FakeBinance) -> None:
    seen: list[tuple[int, int]] = []
    rig = build_rig(fake, on_intent=lambda i: seen.append((i.generation, len(fake.writes()))))
    try:
        await rig.gateway.place_order(approved_entry())
        fake.set_price("BTCUSDT", D(99))
        await rig.gateway.poll()
        fake.set_price("BTCUSDT", D(103))
        before = len(fake.writes())
        await rig.gateway.raise_stop("e-1", D(97))
        await rig.gateway.drop_target("e-1")

        assert seen == [(1, before), (2, before + 2)]
    finally:
        await rig.client.aclose()


async def test_intent_after_drop_target_is_replayed_as_a_dropped_target(fake: FakeBinance) -> None:
    intents: list[Any] = []
    first = build_rig(fake, on_intent=intents.append)
    await first.gateway.place_order(approved_entry())
    fake.set_price("BTCUSDT", D(99))
    await first.gateway.poll()
    fake.set_price("BTCUSDT", D(103))
    original = first.client.request

    async def dies_on_stop_post(m: str, p: str, *a: Any, **k: Any) -> Any:
        if (m, p) == ("POST", "/api/v3/order"):
            raise KeyboardInterrupt
        return await original(m, p, *a, **k)

    first.client.request = dies_on_stop_post  # type: ignore[method-assign]
    with pytest.raises(KeyboardInterrupt):
        await first.gateway.drop_target("e-1")
    second = await restart(first)
    try:
        await second.gateway.recover([intents[-1].persisted(persisted())])

        assert second.gateway.protection_of("e-1") == (D(10), D(95), None)
        assert covered(fake) == D(10)
    finally:
        await second.client.aclose()
