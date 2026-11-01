# ruff: noqa: E501
"""Fechamento da etapa 2 do QA: venda alheia nunca abate posicao propria protegida, atribuicao por entrada,
limite de geracoes, paginacao de /myTrades e cancelamento. Somente a fake local (nada de Binance real)."""

import asyncio
import contextlib
import os
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from engine.domain.gateway import UnknownOrderError
from engine.infrastructure.live import gateway as gateway_module
from engine.infrastructure.live.errors import LiveError
from engine.infrastructure.live.gateway import derived_id
from engine.infrastructure.live.recovery import ProtectionIntent
from tests.fakes.fake_binance import FakeBinance
from tests.unit.live.support import approved_entry, build_rig
from tests.unit.live.test_exits_reconcile import local_position
from tests.unit.live.test_recover import covered, persisted, protected_then_down, restart
from tests.unit.live.test_review_round6 import extra_trade, market_exit, paginate_my_trades
from tests.unit.live.test_review_round7 import manual_sale

D = Decimal
EPS = D("0.0005")
ALIEN = D(os.environ.get("LIVE_FUZZ_ALIEN", "5"))
POLL_AFTER_PRICE = os.environ.get("LIVE_FUZZ_POLL_AFTER_PRICE") == "1"


def own_held(fake: FakeBinance, manual: D | None = None) -> D:
    manual = manual or D(0)
    bought = sum((D(t["qty"]) for t in fake.trades if t["isBuyer"]), D(0))
    sold = sum((D(t["qty"]) for t in fake.trades if not t["isBuyer"]), D(0))
    return bought - (sold - manual)


# ---- 1: venda alheia com a OCO viva nao abate a posicao propria -----------------------------------------------


async def test_foreign_sale_never_hides_own_position_left_without_stop_after_a_full_exit(
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
        assert second.gateway.halted

        with contextlib.suppress(LiveError, UnknownOrderError):
            await second.gateway.place_order(market_exit("7"))

        own_left = own_held(fake, D(3))
        assert not (second.gateway.entry_state("e-1") == "closed" and own_left > 0 and covered(fake) == 0)
        assert second.gateway.entry_state("e-1") == "exposed"
        assert covered(fake) == D(10) and own_left > 0
    finally:
        await second.client.aclose()


# ---- 2: atribuicao por entrada (2 entradas no mesmo simbolo) -------------------------------------------------


def set_entry_fill_time(fake: FakeBinance, cid: str, at_ms: int) -> None:
    order = next(o for o in fake.orders.values() if o.cid == cid)
    for trade in fake.trades:
        if trade["orderId"] == order.id:
            trade["time"] = at_ms


async def two_entries(fake: FakeBinance, sale_at: str, sale_qty: str = "3") -> Any:
    fake.balances["BTC"] = [D(5), D(0)]
    first = build_rig(fake)
    await first.gateway.place_order(approved_entry(cid="e-1"))
    await first.gateway.place_order(approved_entry(cid="e-2"))
    fake.set_price("BTCUSDT", D(99))
    await first.gateway.poll()
    fake.set_price("BTCUSDT", D(103))
    base = fake.trades[-1]["time"]
    set_entry_fill_time(fake, "e-1", base)
    set_entry_fill_time(fake, "e-2", base + (200 if sale_at == "before_e2" else 20))
    await manual_sale(fake, sale_qty)
    fake.trades[-1]["time"] = base + (100 if sale_at == "before_e2" else 500)
    return first


async def test_sale_between_two_entries_only_pauses_the_entry_bought_before_it(fake: FakeBinance) -> None:
    first = await two_entries(fake, "before_e2")
    second = await restart(first)
    try:
        result = await second.gateway.recover([persisted("e-1"), persisted("e-2")])

        assert [r.outcome for r in result.entries] == ["exposed", "protected"], result
        assert covered(fake) == D(20)
        assert second.gateway.entry_state("e-2") == "protected"
    finally:
        await second.client.aclose()


async def test_sale_after_both_entries_is_counted_once_per_entry_never_multiplied(fake: FakeBinance) -> None:
    first = await two_entries(fake, "after_both")
    second = await restart(first)
    try:
        result = await second.gateway.recover([persisted("e-1"), persisted("e-2")])

        assert [r.outcome for r in result.entries] == ["exposed", "exposed"], result
        assert all("(3)" in r.detail for r in result.entries), result
        assert covered(fake) == D(20)
        assert fake.free("BTC") == D(2)
    finally:
        await second.client.aclose()


# ---- 3: limite de geracoes (borda exata) ---------------------------------------------------------------------


async def test_recover_at_the_generation_limit_edge(fake: FakeBinance) -> None:
    first = await protected_then_down(fake)
    second = await restart(first)
    try:
        edge = gateway_module._MAX_GENERATIONS - gateway_module._GENERATION_LOOKAHEAD
        entry = second.gateway._register_recovering(persisted(generation=edge))
        await second.gateway._register_recovered_orders(entry)

        assert entry.generation == gateway_module._MAX_GENERATIONS
    finally:
        await second.client.aclose()


async def test_recover_beyond_the_generation_limit_is_exposed_with_alert(fake: FakeBinance) -> None:
    first = await protected_then_down(fake)
    second = await restart(first)
    try:
        edge = gateway_module._MAX_GENERATIONS - gateway_module._GENERATION_LOOKAHEAD + 1
        result = await second.gateway.recover([persisted(generation=edge)])

        assert [r.outcome for r in result.entries] == ["exposed"], result
        assert "limite" in result.entries[0].detail
        assert second.gateway.halted
        assert covered(fake) == D(10)
    finally:
        await second.client.aclose()


# ---- 4: paginacao de /myTrades -------------------------------------------------------------------------------


def paged_requests(fake: FakeBinance) -> list[Any]:
    return [r for r in fake.log if r.path == "/api/v3/myTrades" and "orderId" not in r.params]


async def test_incomplete_page_ends_pagination_without_an_extra_request(fake: FakeBinance) -> None:
    rig = await protected_then_down(fake)
    try:
        paginate_my_trades(fake, 1000)
        fake.reset_log()

        await rig.gateway.reconcile([local_position()])

        assert len(paged_requests(fake)) == 1
    finally:
        await rig.client.aclose()


async def test_full_page_asks_for_the_next_one_with_from_id(fake: FakeBinance) -> None:
    rig = await protected_then_down(fake)
    try:
        paginate_my_trades(fake, 1000)
        base = fake.trades[-1]["time"]
        for i in range(1100):
            fake.trades.append(extra_trade(10_000 + i, "0.001", buyer=True, at_ms=base + 1 + i))
        fake.reset_log()

        result = await rig.gateway.reconcile([local_position()])

        requests = paged_requests(fake)
        assert [("fromId" in r.params, "startTime" in r.params) for r in requests] == [
            (False, True),
            (True, False),
        ]
        assert requests[0].params["limit"] == "1000"
        assert 11_099 in result.unknown_trades
    finally:
        await rig.client.aclose()


async def test_reconcile_page_cap_degrades_to_inconclusive_with_halt_and_alert(
    fake: FakeBinance, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = await protected_then_down(fake)
    try:
        paginate_my_trades(fake, 1000)
        base = fake.trades[-1]["time"]
        for i in range(1100):
            fake.trades.append(extra_trade(10_000 + i, "0.001", buyer=True, at_ms=base + 1 + i))
        monkeypatch.setattr(gateway_module, "_MAX_TRADE_PAGES", 1)

        result = await rig.gateway.reconcile([local_position()])

        assert result.inconclusive == ("BTC/USDT",)
        assert not result.ok and rig.gateway.halted
        assert [a for a in rig.alerts if a[0] == "critical" and "inconclusiva" in a[1]]
    finally:
        await rig.client.aclose()


async def test_recover_page_cap_leaves_the_entry_exposed_instead_of_raising(
    fake: FakeBinance, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = await protected_then_down(fake)
    paginate_my_trades(fake, 1000)
    base = fake.trades[-1]["time"]
    for i in range(1100):
        fake.trades.append(extra_trade(10_000 + i, "0.001", buyer=True, at_ms=base + 1 + i))
    monkeypatch.setattr(gateway_module, "_MAX_TRADE_PAGES", 1)
    second = await restart(first)
    try:
        result = await second.gateway.recover([persisted()])

        assert [r.outcome for r in result.entries] == ["exposed"], result
        assert covered(fake) == D(10) and second.gateway.halted
    finally:
        await second.client.aclose()


# ---- cancelamento (CancelledError) nos caminhos novos --------------------------------------------------------


async def test_cancelled_error_during_recover_trade_reading_propagates_and_leaves_protection_alone(
    fake: FakeBinance,
) -> None:
    first = await protected_then_down(fake)
    second = await restart(first)
    original = second.client.request

    async def cancelled(m: str, p: str, params: Any = None, *a: Any, **k: Any) -> Any:
        if p == "/api/v3/myTrades" and params and "orderId" not in params:
            raise asyncio.CancelledError
        return await original(m, p, params, *a, **k)

    second.client.request = cancelled  # type: ignore[method-assign]
    try:
        with pytest.raises(asyncio.CancelledError):
            await second.gateway.recover([persisted()])
        assert covered(fake) == D(10)
        second.client.request = original  # type: ignore[method-assign]
        result = await (await restart(second)).gateway.recover([persisted()])
        assert [r.outcome for r in result.entries] == ["protected"], result
    finally:
        await second.client.aclose()


@pytest.mark.parametrize("operation", ["poll", "raise", "exit"])
@pytest.mark.parametrize("at", range(0, 16))
async def test_cancelled_error_at_every_request_never_leaves_own_position_unprotected(
    fake: FakeBinance, operation: str, at: int
) -> None:
    fake.balances["BTC"] = [ALIEN, D(0)]
    intents: list[ProtectionIntent] = []
    first = build_rig(fake, on_intent=intents.append)
    await first.gateway.place_order(approved_entry())
    fake.set_price("BTCUSDT", D(99))
    await first.gateway.poll()
    fake.set_price("BTCUSDT", D(103))
    original = first.client.request
    count = [0]

    async def cancel_at(m: str, p: str, *a: Any, **k: Any) -> Any:
        if count[0] == at:
            count[0] += 1
            raise asyncio.CancelledError
        count[0] += 1
        return await original(m, p, *a, **k)

    first.client.request = cancel_at  # type: ignore[method-assign]
    with contextlib.suppress(asyncio.CancelledError, LiveError, UnknownOrderError):
        match operation:
            case "poll":
                await first.gateway.poll()
            case "raise":
                await first.gateway.raise_stop("e-1", D(98))
            case "exit":
                await first.gateway.place_order(market_exit("4"))
    second = await restart(first)
    try:
        base = persisted()
        await second.gateway.recover([intents[-1].persisted(base) if intents else base])

        assert own_held(fake) <= EPS or covered(fake) > 0 or second.gateway.halted
        assert fake.free("BTC") + fake.locked("BTC") >= ALIEN - EPS
    finally:
        await second.client.aclose()


# ---- propriedade: stop parcial + venda alheia + intents + 2 entradas -----------------------------------------


async def recover_scenario(
    partial: int, manual: int, raise_e2: bool, exit_qty: int, swap: bool
) -> str | None:
    manual = min(manual, int(ALIEN))
    fake = FakeBinance()
    await fake.start()
    fake.balances["BTC"] = [ALIEN, D(0)]
    intents: list[ProtectionIntent] = []
    first = build_rig(fake, on_intent=intents.append)
    try:
        await first.gateway.place_order(approved_entry(cid="e-1"))
        await first.gateway.place_order(approved_entry(cid="e-2"))
        fake.set_price("BTCUSDT", D(99))
        if partial:
            leg = next(o for o in fake.orders.values() if o.cid == derived_id("e-1", "S0"))
            fake._fill(leg, D(partial), D(94), maker=False)
        if POLL_AFTER_PRICE:
            await first.gateway.poll()
        fake.set_price("BTCUSDT", D(103))
        if raise_e2:
            with contextlib.suppress(LiveError, UnknownOrderError):
                await first.gateway.raise_stop("e-2", D(97))
        if manual:
            await manual_sale(fake, str(manual))
        second = await restart(first)
        try:
            by_entry = {i.entry_id: i for i in intents}
            bases = [persisted("e-1"), persisted("e-2")]
            bases = [
                by_entry[b.order.client_order_id].persisted(b) if b.order.client_order_id in by_entry else b
                for b in bases
            ]
            await second.gateway.recover(bases[::-1] if swap else bases)
            if exit_qty:
                with contextlib.suppress(LiveError, UnknownOrderError):
                    await second.gateway.place_order(market_exit(str(exit_qty)))
            for _ in range(2):
                with contextlib.suppress(LiveError):
                    await second.gateway.poll()
            sold = sum((D(t["qty"]) for t in fake.trades if not t["isBuyer"]), D(0))
            bought = sum((D(t["qty"]) for t in fake.trades if t["isBuyer"]), D(0))
            if (
                sold > bought + D(manual) + EPS
                or fake.free("BTC") + fake.locked("BTC") < ALIEN - D(manual) - EPS
            ):
                return "vendeu/tocou saldo alheio"
            held = own_held(fake, D(manual))
            if held > EPS and covered(fake) <= 0 and not second.gateway.halted:
                return f"posicao propria {held} sem stop e sem pausa"
            return None
        finally:
            await second.client.aclose()
    finally:
        await fake.stop()


@settings(
    max_examples=int(os.environ.get("LIVE_FUZZ_EXAMPLES", "60")),
    deadline=None,
    suppress_health_check=list(HealthCheck),
)
@given(
    partial=st.sampled_from([0, 1, 4, 9]),
    manual=st.integers(0, 5),
    raise_e2=st.booleans(),
    exit_qty=st.integers(0, 12),
    swap=st.booleans(),
)
def test_recover_with_partial_stop_foreign_sale_intents_and_two_entries_keeps_every_invariant(
    partial: int, manual: int, raise_e2: bool, exit_qty: int, swap: bool
) -> None:
    problem = asyncio.run(recover_scenario(partial, manual, raise_e2, exit_qty, swap))

    assert problem is None, problem
