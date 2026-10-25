# ruff: noqa: E501
"""5a revisao QA do 7B-1 (recover/close_orphan/_fresh_owed). Somente a fake local.

Testes `xfail(strict=True)` com prefixo `BUG:` sao EVIDENCIA de defeitos reais da 5a rodada.
"""

import asyncio
from decimal import Decimal
from typing import Any

import pytest

from engine.domain.order import ExitOrder, OrderType
from engine.domain.risk import RiskConfig
from engine.infrastructure.live.client import BinanceRestClient
from engine.infrastructure.live.gateway import LiveGateway
from engine.infrastructure.live.recovery import PersistedEntry
from tests.fakes.fake_binance import ApiError, FakeBinance, Req
from tests.unit.live.support import (
    NOW,
    SYMBOL,
    Rig,
    approved_entry,
    build_rig,
    fake_credentials,
    live_config,
    no_sleep,
)
from tests.unit.live.test_recover import (
    confirmation,
    covered,
    expose_with_failures,
    persisted,
    protected_then_down,
    restart,
)
from tests.unit.live.test_review_round4 import settle

D = Decimal


def market_exit(qty: str, cid: str = "x-1") -> ExitOrder:
    return ExitOrder(
        client_order_id=cid, symbol=SYMBOL, type=OrderType.MARKET, quantity=D(qty), created_at=NOW
    )


def reuse_closed_ids(fake: FakeBinance) -> None:
    """Binance: client id so precisa ser unico entre ordens ABERTAS; um id de ordem encerrada pode ser reusado
    (GET devolve a mais recente). A fake rejeita qualquer duplicata, o que esconde colisoes com ids mortos."""

    def check_cid(cid: str) -> None:
        if any(
            o.cid == cid and o.status in ("NEW", "PARTIALLY_FILLED", "PENDING_NEW")
            for o in fake.orders.values()
        ):
            raise ApiError(400, -2010, "Duplicate order sent.")

    def check_list(cid: str) -> None:
        if any(item.cid == cid and item.status == "EXECUTING" for item in fake.lists.values()):
            raise ApiError(400, -2010, "Duplicate order sent.")

    def find(params: dict[str, str]) -> Any:
        cid = params.get("origClientOrderId")
        hits = [o for o in fake.orders.values() if o.cid == cid and o.symbol == params["symbol"]]
        if not hits:
            raise ApiError(400, -2013, "Order does not exist.")
        return hits[-1]

    def find_list(params: dict[str, str]) -> Any:
        cid = params.get("listClientOrderId") or params.get("origClientOrderId")
        hits = [item for item in fake.lists.values() if item.cid == cid]
        if not hits:
            raise ApiError(400, -2013, "Order list does not exist.")
        return hits[-1]

    fake._check_cid = check_cid  # type: ignore[method-assign]
    fake._check_list_cid = check_list  # type: ignore[method-assign]
    fake._find = find  # type: ignore[method-assign]
    fake._find_list = find_list  # type: ignore[method-assign]


# ---- recover: geracao de protecao atrasada (stop de arrasto) ---------------------------------------------


@pytest.mark.xfail(
    strict=True, reason="BUG: recover so sonda ate generation+2; trailing stop gera mais geracoes"
)
async def test_bug_restart_after_trailing_stop_raises_adopts_the_latest_protection_with_a_stale_generation(
    fake: FakeBinance,
) -> None:
    fake.balances["BTC"] = [D(5), D(0)]
    reuse_closed_ids(fake)
    first = build_rig(fake)
    await first.gateway.place_order(approved_entry())
    fake.set_price("BTCUSDT", D(99))
    await first.gateway.poll()
    fake.set_price("BTCUSDT", D(105))
    for stop in (96, 97, 98, 99, 100):  # 5 raise_stop => geracao 5; o chamador nao tem como ler a geracao
        await first.gateway.raise_stop("e-1", D(stop))
    assert covered(fake) == D(10)
    second = await restart(first)
    try:
        result = await second.gateway.recover([persisted(generation=0)])

        assert fake.free("BTC") == D(5), f"recover travou saldo ALHEIO sob um stop do robo: {result}"
        assert covered(fake) == D(10), "protecao duplicada/adulterada na corretora"
        assert [r.outcome for r in result.entries] == ["protected"], result
        assert not second.gateway.halted
    finally:
        await second.client.aclose()


# ---- recover: orcamento de exposicao consumido pela propria leitura ------------------------------------


async def test_recover_does_not_dump_a_protectable_position_because_its_own_reads_were_slow(
    fake: FakeBinance,
) -> None:
    fake.fail_activation = True
    first = build_rig(fake)
    await first.gateway.place_order(approved_entry())
    fake.set_price("BTCUSDT", D(99))  # compra enche; OCO nao ativou; primeiro gateway morre antes do poll
    await first.client.aclose()
    fake.fail_activation = False
    fake.reset_log()

    config = live_config(fake)
    client = BinanceRestClient(config, fake_credentials(), sleep=no_sleep)
    clock = [0.0]
    fake.after_dispatch = lambda _: clock.__setitem__(0, clock[0] + 3.0)  # 3 s por requisicao
    gateway = LiveGateway(
        config,
        client,
        risk_config=RiskConfig(taker_fee_rate=D(0)),
        now=lambda: NOW,
        monotonic=lambda: clock[0],
        sleep=no_sleep,
    )
    try:
        result = await gateway.recover([persisted()])

        assert [r.outcome for r in result.entries] == ["reprotected"], result
        assert not [r for r in fake.order_writes() if r.params.get("type") == "MARKET"], (
            "recover vendeu a mercado uma posicao que ainda podia ser protegida"
        )
        assert covered(fake) == D(10)
    finally:
        await client.aclose()


# ---- myTrades paginado ---------------------------------------------------------------------------------


def paginate_my_trades(fake: FakeBinance, limit: int = 500) -> None:
    """Emula a Binance: sem orderId, ate `limit` trades por chamada (ordenados por id), `fromId` pagina."""
    original = fake._my_trades

    def paged(params: dict[str, str]) -> list[dict[str, Any]]:
        found = original(params)
        if "orderId" in params:
            return found
        found = sorted(found, key=lambda t: int(t["id"]))
        if "fromId" in params:
            found = [t for t in found if int(t["id"]) >= int(params["fromId"])]
        return found[:limit]

    fake._my_trades = paged  # type: ignore[method-assign]


def extra_trade(trade_id: int, qty: str, *, buyer: bool, at_ms: int) -> dict[str, Any]:
    return {
        "symbol": "BTCUSDT",
        "id": trade_id,
        "orderId": 900_000 + trade_id,
        "price": "103",
        "qty": qty,
        "quoteQty": "1",
        "commission": "0",
        "commissionAsset": "USDT",
        "time": at_ms,
        "isBuyer": buyer,
        "isMaker": False,
    }


@pytest.mark.xfail(
    strict=True, reason="BUG: _check_unattributed_sells le so a 1a pagina de /myTrades (limite 500)"
)
async def test_bug_unattributed_sale_beyond_the_first_mytrades_page_is_detected(fake: FakeBinance) -> None:
    fake.balances["BTC"] = [D(5), D(0)]
    first = await protected_then_down(fake)
    paginate_my_trades(fake, 500)
    base = fake.trades[-1]["time"]
    for i in range(600):  # trades de OUTRO uso da conta depois da compra do robo
        fake.trades.append(extra_trade(10_000 + i, "0.001", buyer=True, at_ms=base + 1 + i))
    fake.trades.append(extra_trade(20_000, "4", buyer=False, at_ms=base + 1000))
    second = await restart(first)
    try:
        result = await second.gateway.recover([persisted()])

        assert [r.outcome for r in result.entries] == ["exposed"], result
    finally:
        await second.client.aclose()


# ---- close_orphan: idempotencia / lote / concorrencia --------------------------------------------------


async def orphan(fake: FakeBinance) -> Rig:
    first = build_rig(fake, protection_retries=1)
    await expose_with_failures(fake, first)
    second = await restart(first, protection_retries=1)
    result = await second.gateway.recover([persisted()])
    assert [r.outcome for r in result.entries] == ["exposed"], result
    fake.faults.clear()
    fake.fail_activation = False
    return second


async def test_close_orphan_called_repeatedly_never_sells_more_than_the_position_nor_foreign_balance(
    fake: FakeBinance,
) -> None:
    fake.balances["BTC"] = [D(5), D(0)]
    rig = await orphan(fake)
    try:
        for _ in range(4):
            await settle(lambda: rig.gateway.close_orphan(SYMBOL, D(4), confirmation("4")))
        sold = sum((D(t["qty"]) for t in fake.trades if not t["isBuyer"]), D(0))
        assert sold == D(10)
        assert fake.free("BTC") == D(5)
    finally:
        await rig.client.aclose()


async def test_close_orphan_concurrent_calls_do_not_double_sell(fake: FakeBinance) -> None:
    fake.balances["BTC"] = [D(5), D(0)]
    rig = await orphan(fake)
    try:
        results = await asyncio.gather(
            *(rig.gateway.close_orphan(SYMBOL, D(10), confirmation("10")) for _ in range(3)),
            return_exceptions=True,
        )

        assert fake.free("BTC") == D(5), results
    finally:
        await rig.client.aclose()


async def test_close_orphan_respects_market_max_qty_and_repeats_without_overselling(
    fake: FakeBinance,
) -> None:
    fake.balances["BTC"] = [D(5), D(0)]
    fake.market_max_qty = D(3)
    rig = await orphan(fake)
    try:
        for _ in range(6):
            await settle(lambda: rig.gateway.close_orphan(SYMBOL, D(10), confirmation("10")))

        assert fake.free("BTC") == D(5)
        sold = sum((D(t["qty"]) for t in fake.trades if not t["isBuyer"]), D(0))
        assert sold == D(10)
    finally:
        await rig.client.aclose()


async def test_close_orphan_with_lost_response_does_not_oversell_on_retry(fake: FakeBinance) -> None:
    fake.balances["BTC"] = [D(5), D(0)]
    rig = await orphan(fake)
    try:
        original = rig.client.request
        state = {"armed": True}

        async def wrapper(m: str, p: str, *a: Any, **k: Any) -> Any:
            result = await original(m, p, *a, **k)
            if state["armed"] and (m, p) == ("POST", "/api/v3/order"):
                state["armed"] = False
                raise asyncio.CancelledError  # resposta perdida: a ordem EXECUTOU
            return result

        rig.client.request = wrapper  # type: ignore[method-assign]
        with pytest.raises(asyncio.CancelledError):
            await rig.gateway.close_orphan(SYMBOL, D(10), confirmation("10"))
        rig.client.request = original  # type: ignore[method-assign]
        for _ in range(3):
            await settle(lambda: rig.gateway.close_orphan(SYMBOL, D(10), confirmation("10")))
        await settle(rig.gateway.poll)

        assert fake.free("BTC") == D(5), "venda duplicada apos resposta perdida"
    finally:
        await rig.client.aclose()


# ---- _fresh_owed: duas entradas no mesmo simbolo, comissao em base --------------------------------------


async def test_two_entries_same_symbol_exit_and_stop_never_touch_each_other_or_foreign_balance(
    fake: FakeBinance,
) -> None:
    fake.balances["BTC"] = [D(3), D(0)]
    fake.commission_in_base = True
    rig = build_rig(fake)
    try:
        await rig.gateway.place_order(approved_entry(cid="e-1"))
        await rig.gateway.place_order(approved_entry(cid="e-2"))
        fake.set_price("BTCUSDT", D(99))
        await rig.gateway.poll()
        await settle(lambda: rig.gateway.place_order(market_exit("9.9", "x-1")))
        fake.set_price("BTCUSDT", D(94))
        for _ in range(3):
            await settle(rig.gateway.poll)
        bought = sum((D(t["qty"]) for t in fake.trades if t["isBuyer"]), D(0))
        sold = sum((D(t["qty"]) for t in fake.trades if not t["isBuyer"]), D(0))
        assert sold <= bought
        assert fake.free("BTC") + fake.locked("BTC") >= D(3)
    finally:
        await rig.client.aclose()


# ---- injecao em CADA requisicao: recover e close_orphan ---------------------------------------------------


async def inject_scenario(at: int, after: bool, trigger: int, phase: str) -> str | None:
    fake = FakeBinance()
    await fake.start()
    fake.balances["BTC"] = [D(5), D(0)]
    first = build_rig(fake, protection_retries=1)
    try:
        await first.gateway.place_order(approved_entry())
        fake.set_price("BTCUSDT", D(99))
        await settle(first.gateway.poll)
        fake.set_price("BTCUSDT", D(103))
        second = await restart(first)
        count = [0]

        def hook(_: Req) -> None:
            if count[0] == at:
                fake.set_price("BTCUSDT", D(trigger))
            count[0] += 1

        if after:
            fake.after_dispatch = hook
        else:
            fake.before_dispatch = hook
        try:
            await settle(lambda: second.gateway.recover([persisted()]))
            if phase == "orphan":
                await settle(lambda: second.gateway.close_orphan(SYMBOL, D(10), confirmation("10")))
            fake.before_dispatch = fake.after_dispatch = None
            for _ in range(3):
                await settle(second.gateway.poll)
            bought = sum((D(t["qty"]) for t in fake.trades if t["isBuyer"]), D(0))
            sold = sum((D(t["qty"]) for t in fake.trades if not t["isBuyer"]), D(0))
            if sold > bought + D("0.0005") or fake.free("BTC") + fake.locked("BTC") < D(5) - D("0.0005"):
                return f"vendeu {sold} de {bought}; alheio={fake.free('BTC') + fake.locked('BTC')}"
            return None
        finally:
            await second.client.aclose()
    finally:
        await fake.stop()


@pytest.mark.parametrize("phase", ["recover", "orphan"])
@pytest.mark.parametrize("trigger", [112, 94])
@pytest.mark.parametrize("after", [False, True])
@pytest.mark.parametrize("at", range(0, 45))
async def test_execution_at_every_request_during_recover_never_sells_foreign_balance(
    at: int, after: bool, trigger: int, phase: str
) -> None:
    problem = await inject_scenario(at, after, trigger, phase)

    assert problem is None, problem


# ---- concorrencia: poll + saida + raise_stop + close_orphan em paralelo, com execucao injetada -------------


async def concurrent_scenario(
    at: int, after: bool, trigger: int, two_entries: bool, base_fee: bool
) -> str | None:
    fake = FakeBinance()
    await fake.start()
    fake.balances["BTC"] = [D(5), D(0)]
    fake.commission_in_base = base_fee
    rig = build_rig(fake, request_timeout_seconds=0.5, read_retries=1, protection_activation_grace_seconds=0)
    try:
        await rig.gateway.place_order(approved_entry(cid="e-1"))
        if two_entries:
            await rig.gateway.place_order(approved_entry(cid="e-2"))
        fake.set_price("BTCUSDT", D(99))
        count = [0]

        def hook(_: Req) -> None:
            if count[0] == at:
                fake.set_price("BTCUSDT", D(trigger))
            count[0] += 1

        if after:
            fake.after_dispatch = hook
        else:
            fake.before_dispatch = hook
        await asyncio.gather(
            settle(rig.gateway.poll),
            settle(lambda: rig.gateway.place_order(market_exit("5", "x-a"))),
            settle(lambda: rig.gateway.raise_stop("e-1", D(98))),
            settle(rig.gateway.poll),
            settle(lambda: rig.gateway.place_order(market_exit("4", "x-b"))),
        )
        fake.before_dispatch = fake.after_dispatch = None
        for _ in range(3):
            await settle(rig.gateway.poll)
        bought = sum((D(t["qty"]) for t in fake.trades if t["isBuyer"]), D(0))
        sold = sum((D(t["qty"]) for t in fake.trades if not t["isBuyer"]), D(0))
        if sold > bought + D("0.0005") or fake.free("BTC") + fake.locked("BTC") < D(5) - D("0.0005"):
            return f"vendeu {sold} de {bought}; alheio={fake.free('BTC') + fake.locked('BTC')}"
        return None
    finally:
        await rig.client.aclose()
        await fake.stop()


@pytest.mark.parametrize("base_fee", [False, True])
@pytest.mark.parametrize("two_entries", [False, True])
@pytest.mark.parametrize("trigger", [112, 94])
@pytest.mark.parametrize("after", [False, True])
@pytest.mark.parametrize("at", range(0, 60, 2))
async def test_concurrent_operations_with_injected_execution_never_sell_foreign_balance(
    at: int, after: bool, trigger: int, two_entries: bool, base_fee: bool
) -> None:
    problem = await concurrent_scenario(at, after, trigger, two_entries, base_fee)

    assert problem is None, problem


# ---- reinicio (morte do processo) em CADA requisicao de cada operacao -------------------------------------


class Kill(BaseException):
    """Morte do processo: nenhuma resposta chega e nada mais e executado."""


def arm_kill(rig: Rig, at: int, after: bool) -> list[int]:
    original = rig.client.request
    count = [0]

    async def wrapper(m: str, p: str, *a: Any, **k: Any) -> Any:
        if count[0] < 0:
            raise Kill
        index = count[0]
        count[0] += 1
        if index == at:
            count[0] = -1
            if after:
                await original(m, p, *a, **k)
            raise Kill
        return await original(m, p, *a, **k)

    rig.client.request = wrapper  # type: ignore[method-assign]
    return count


async def restart_scenario(op: str, at: int, after: bool) -> str | None:
    fake = FakeBinance()
    await fake.start()
    fake.balances["BTC"] = [D(5), D(0)]
    reuse_closed_ids(fake)
    first = build_rig(
        fake, protection_retries=1, partial_fill_grace_seconds=0, protection_activation_grace_seconds=0
    )
    try:
        approved = approved_entry()
        exits: tuple[str, ...] = ()
        if op == "place":
            arm_kill(first, at, after)
            await settle(lambda: first.gateway.place_order(approved))
        else:
            if op == "partial":
                await first.gateway.place_order(approved)
                fake.fill_fraction = D("0.5")
                fake.fail(
                    "/api/v3/orderList", method="DELETE", times=1000, kind="binance", status=400, code=-1000
                )
                fake.set_price("BTCUSDT", D(99))
                arm_kill(first, at, after)
                await settle(first.gateway.poll)
                fake.faults.clear()
            elif op == "emergency":
                fake.fail_activation = True
                await first.gateway.place_order(approved)
                fake.fail(
                    "/api/v3/orderList/oco", method="POST", times=2, kind="binance", status=400, code=-1000
                )
                fake.fail("/api/v3/order", method="POST", times=2, kind="binance", status=400, code=-1000)
                fake.set_price("BTCUSDT", D(99))
                arm_kill(first, at, after)
                await settle(first.gateway.poll)
                fake.fail_activation = False
            else:
                await first.gateway.place_order(approved)
                fake.set_price("BTCUSDT", D(99))
                await first.gateway.poll()
                fake.set_price("BTCUSDT", D(103))
                arm_kill(first, at, after)
                if op == "raise":
                    await settle(lambda: first.gateway.raise_stop("e-1", D(98)))
                elif op == "drop":
                    await settle(lambda: first.gateway.drop_target("e-1"))
                elif op == "exit":
                    exits = ("x-1",)
                    await settle(lambda: first.gateway.place_order(market_exit("4", "x-1")))
        fake.faults.clear()
        await first.client.aclose()
        second = build_rig(fake, protection_retries=1, partial_fill_grace_seconds=0)
        mark = len(fake.log)
        try:
            entry = PersistedEntry(
                order=approved.order, expires_at=approved.guards.expires_at, exit_client_ids=exits
            )
            await settle(lambda: second.gateway.recover([entry]))
            for _ in range(3):
                await settle(second.gateway.poll)
            bought = sum((D(t["qty"]) for t in fake.trades if t["isBuyer"]), D(0))
            sold = sum((D(t["qty"]) for t in fake.trades if not t["isBuyer"]), D(0))
            entry_posts = [
                r
                for r in fake.log[mark:]
                if r.method == "POST" and (r.path.endswith("/opoco") or r.params.get("side") == "BUY")
            ]
            if entry_posts:
                return f"recover/poll reenviou ENTRADA: {entry_posts}"
            if sold > bought + D("0.0005") or fake.free("BTC") + fake.locked("BTC") < D(5) - D("0.0005"):
                return f"vendeu {sold} de {bought}; alheio={fake.free('BTC') + fake.locked('BTC')}"
            if fake.uncovered_base() > D(5) + D("0.0005") and not (
                second.gateway.halted and any(lvl == "critical" for lvl, _ in second.alerts)
            ):
                return f"posicao descoberta ({fake.uncovered_base() - D(5)}) sem pausa/alerta; estado={second.gateway.open_entry_ids()}"
            return None
        finally:
            await second.client.aclose()
    finally:
        await fake.stop()


@pytest.mark.parametrize("after", [False, True])
@pytest.mark.parametrize("op", ["place", "partial", "emergency", "raise", "drop", "exit"])
@pytest.mark.parametrize("at", range(0, 36))
async def test_process_death_at_every_request_then_recover_never_leaves_an_unprotected_or_foreign_sale(
    at: int, op: str, after: bool
) -> None:
    problem = await restart_scenario(op, at, after)

    assert problem is None, problem


# ---- perna de stop PARCIALMENTE executada (STOP_LOSS_LIMIT em livro raso) ---------------------------------


async def partially_filled_stop_leg(fake: FakeBinance, rig: Rig) -> None:
    """Compra enche, OPOCO ativa; o stop executa 4 de 10 e as 6 restantes seguem vivas (travadas)."""
    from engine.infrastructure.live.gateway import derived_id

    await rig.gateway.place_order(approved_entry())
    fake.set_price("BTCUSDT", D(99))
    stop_leg = next(o for o in fake.orders.values() if o.cid == derived_id("e-1", "S0"))
    fake._fill(stop_leg, D(4), D(94), maker=False)
    assert stop_leg.status == "PARTIALLY_FILLED" and fake.locked("BTC") == D(6)


@pytest.mark.xfail(
    strict=True,
    reason="BUG: perna de stop PARTIALLY_FILLED nao e tratada como viva: poll recoloca OCO sobre saldo ALHEIO",
)
async def test_bug_poll_with_a_partially_filled_stop_leg_does_not_lock_foreign_balance(
    fake: FakeBinance,
) -> None:
    fake.balances["BTC"] = [D(5), D(0)]
    rig = build_rig(fake)
    try:
        await partially_filled_stop_leg(fake, rig)

        await settle(rig.gateway.poll)

        assert fake.locked("BTC") <= D(6), (
            f"travou saldo alheio sob stop do robo (locked={fake.locked('BTC')})"
        )
    finally:
        await rig.client.aclose()


@pytest.mark.xfail(
    strict=True, reason="BUG: saida apos perna de stop PARTIALLY_FILLED vende saldo ALHEIO (clamp usa free)"
)
async def test_bug_exit_with_a_partially_filled_stop_leg_does_not_sell_foreign_balance(
    fake: FakeBinance,
) -> None:
    fake.balances["BTC"] = [D(5), D(0)]
    rig = build_rig(fake)
    try:
        await partially_filled_stop_leg(fake, rig)

        await settle(lambda: rig.gateway.place_order(market_exit("6")))

        assert fake.free("BTC") == D(5), (
            f"saida vendeu saldo ALHEIO (livre agora {fake.free('BTC')}); perna de stop com 6 segue viva"
        )
    finally:
        await rig.client.aclose()


async def test_recover_with_a_partially_filled_stop_leg_keeps_foreign_balance_free(fake: FakeBinance) -> None:
    fake.balances["BTC"] = [D(5), D(0)]
    first = build_rig(fake)
    await partially_filled_stop_leg(fake, first)
    second = await restart(first)
    try:
        await settle(lambda: second.gateway.recover([persisted()]))
        for _ in range(2):
            await settle(second.gateway.poll)

        assert fake.locked("BTC") <= D(6) and fake.free("BTC") >= D(5), (
            f"alheio travado: free={fake.free('BTC')} locked={fake.locked('BTC')}"
        )
    finally:
        await second.client.aclose()


# ---- stop de arrasto perdido na janela do replace: recover reprotege com o stop ORIGINAL -----------------


@pytest.mark.xfail(
    strict=True,
    reason="BUG: PersistedEntry nao leva o stop vigente; reinicio entre DELETE e POST do raise_stop rebaixa o stop",
)
async def test_bug_restart_between_cancel_and_post_of_raise_stop_does_not_lower_the_stop(
    fake: FakeBinance,
) -> None:
    first = await protected_then_down(fake)
    original = first.client.request

    async def dies_on_oco_post(m: str, p: str, *a: Any, **k: Any) -> Any:
        if (m, p) == ("POST", "/api/v3/orderList/oco"):
            raise Kill
        return await original(m, p, *a, **k)

    first.client.request = dies_on_oco_post  # type: ignore[method-assign]
    await settle(
        lambda: first.gateway.raise_stop("e-1", D(100))
    )  # DELETE executou; o novo OCO (stop 100) nao saiu
    assert covered(fake) == 0
    second = await restart(first)
    try:
        await second.gateway.recover([persisted()])

        stops = [o.stop for o in fake.active_stops()]
        assert stops and min(stops) >= D(100), (
            f"stop rebaixado para {stops} apos o reinicio (devia ser >= 100)"
        )
    finally:
        await second.client.aclose()


# ---- venda manual alheia com a protecao do robo VIVA: beco sem saida operacional ---------------------------


@pytest.mark.xfail(
    strict=True,
    reason="BUG: venda manual alheia + OCO viva => recover nao adota (exposed) e close_orphan recusa: sem saida",
)
async def test_bug_unrelated_manual_sale_does_not_strand_a_position_that_is_protected_on_the_exchange(
    fake: FakeBinance,
) -> None:
    fake.balances["BTC"] = [D(5), D(0)]
    first = await protected_then_down(fake)
    fake._post_order(
        {
            "symbol": "BTCUSDT",
            "type": "MARKET",
            "side": "SELL",
            "quantity": "3",
            "newClientOrderId": "manual-1",
        }
    )
    second = await restart(first)
    try:
        result = await second.gateway.recover([persisted()])

        assert [r.outcome for r in result.entries] == ["protected"], result
    finally:
        await second.client.aclose()
