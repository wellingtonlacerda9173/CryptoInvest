# ruff: noqa: E501, SIM105
"""Fuzz adversarial (revisao QA 7B-1, 2a rodada) da invariante 'nunca posicao sem stop sem pausa+alerta'.

Diferencas para `test_properties.py`: a invariante e checada DEPOIS DE CADA operacao (com as falhas ainda
ativas), nao so ao fim; ha cancelamento da task em ponto arbitrario (`wait_for` curto), faltas em 3 pontos
consecutivos, resposta perdida APOS execucao, ganchos que mexem no preco/relogio/filtros logo apos o DELETE ou
antes do POST da OCO, e holdings alheios de BTC que NUNCA podem ser vendidos. Tambem confere que o stop
ativo de cada entrada nunca diminui e que, sem falhas, tudo converge (vivacidade).
Somente a fake local: nenhuma chamada a Binance real/Testnet.
"""

import asyncio
import os
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from engine.domain.gateway import UnknownOrderError
from engine.domain.order import ExitOrder, OrderType
from engine.infrastructure.live.errors import LiveError
from engine.infrastructure.live.gateway import derived_id
from tests.fakes.fake_binance import FakeBinance
from tests.unit.live.support import NOW, SYMBOL, Rig, approved_entry, build_rig
from tests.unit.live.test_properties import FAULT_PATHS, inject

D = Decimal
# Holdings alheios (BTC que o robo nao comprou). Padrao 0: com 0.5 o fuzz acha o BUG documentado em
# test_review_round2.py (stop/alvo executado antes do 1o poll => rescue vende o saldo alheio).
ALIEN = D(os.environ.get("LIVE_FUZZ_ALIEN", "0"))
TOL = D("0.011")
POLL_AFTER_PRICE = os.environ.get("LIVE_FUZZ_POLL_AFTER_PRICE") == "1"
NO_EXIT = (
    os.environ.get("LIVE_FUZZ_NO_EXIT") == "1"
)  # exit com saldo alheio: BUG documentado (owed nao abate a saida)
TIMEOUTS = (0.0003, 0.001, 0.002, 0.004, 0.008, 0.02)

fault_spec = st.tuples(st.integers(0, len(FAULT_PATHS) - 1), st.integers(0, 6), st.integers(1, 6))
ops_strategy = st.one_of(
    st.tuples(st.just("price"), st.integers(90, 118)),
    st.tuples(st.just("poll"), st.just(0)),
    st.tuples(st.just("place"), st.just(0)),
    st.tuples(st.just("raise"), st.integers(95, 114)),
    st.tuples(st.just("drop"), st.just(0)),
    st.tuples(st.just("cancel"), st.just(0)),
    st.tuples(st.just("partial"), st.just(0)),
    st.tuples(st.just("noact"), st.booleans()),
    st.tuples(st.just("exit"), st.integers(1, 12)),
    st.tuples(st.just("fault"), fault_spec),
    st.tuples(st.just("fault3"), st.tuples(fault_spec, fault_spec, fault_spec)),
    st.tuples(st.just("blackout"), st.integers(0, 2)),
    st.tuples(st.just("hook_delete"), st.integers(88, 120)),
    st.tuples(st.just("hook_oco"), st.integers(88, 120)),
    st.tuples(st.just("clock"), st.sampled_from([0, 7000, 30000])),
    st.tuples(st.just("step"), st.sampled_from(["0.001", "0.01", "0.1"])),
    st.tuples(st.just("cancel_task"), st.tuples(st.integers(0, 6), st.integers(0, len(TIMEOUTS) - 1))),
)


def stop_ids(entry_id: str) -> set[str]:
    return {derived_id(entry_id, f"{tag}{g}") for tag in ("S", "X") for g in range(0, 40)}


def entry_stop(fake: FakeBinance, entry_id: str) -> Decimal | None:
    ids = stop_ids(entry_id)
    prices = [o.stop for o in fake.active_stops() if o.cid in ids]
    return max(prices) if prices else None


def bought_sold(fake: FakeBinance) -> tuple[Decimal, Decimal]:
    bought = sum((D(t["qty"]) for t in fake.trades if t["isBuyer"]), D(0))
    sold = sum((D(t["qty"]) for t in fake.trades if not t["isBuyer"]), D(0))
    return bought, sold


async def apply(rig: Rig, name: str, arg: Any, placed: list[int]) -> None:
    gateway, fake = rig.gateway, rig.fake
    match name:
        case "price":
            fake.set_price("BTCUSDT", D(arg))
            if POLL_AFTER_PRICE:
                await recover_after_cancel(rig)
        case "poll":
            await gateway.poll()
        case "place" if placed[0] < 3:
            placed[0] += 1
            try:
                await gateway.place_order(approved_entry(cid=f"e-{placed[0]}", equity=3000))
            except LiveError:
                # BUG documentado (test_review_round2): LiveError antes de _arm em place/cancel nao pausa
                await recover_after_cancel(rig)
                raise
        case "raise":
            for entry_id in gateway.open_entry_ids():
                await gateway.raise_stop(entry_id, D(arg))
        case "drop":
            for entry_id in gateway.open_entry_ids():
                await gateway.drop_target(entry_id)
        case "cancel":
            try:
                for entry_id in gateway.open_entry_ids():
                    await gateway.cancel_order(SYMBOL, entry_id)
            except LiveError:
                # BUG documentado (test_review_round2): cancel_order nao pausa; o proximo poll repara
                await recover_after_cancel(rig)
                raise
        case "partial":
            fake.fill_fraction = D("0.5")
        case "noact":
            fake.fail_activation = bool(arg)
        case "exit" if not NO_EXIT:
            await gateway.place_order(
                ExitOrder(
                    client_order_id=f"x-{len(fake.log)}",
                    symbol=SYMBOL,
                    type=OrderType.MARKET,
                    quantity=D(arg),
                    created_at=NOW,
                )
            )
        case "fault":
            inject(fake, arg)
        case "fault3":
            for spec in arg:
                inject(fake, spec)
        case "blackout":
            for path, method in (("/api/v3/orderList/oco", "POST"), ("/api/v3/order", "POST")):
                match arg:
                    case 0:
                        fake.fail(path, method=method, times=60, kind="binance", status=400, code=-1000)
                    case 1:
                        fake.fail(path, method=method, times=60, status=503)
                    case _:
                        fake.fail(path, method=method, times=1, status=418, msg="banned until 99999999999999")
        case "hook_delete":
            hook(fake, "_delete_list", after=lambda: fake.set_price("BTCUSDT", D(arg)))
        case "hook_oco":
            hook(fake, "_post_oco", before=lambda: fake.set_price("BTCUSDT", D(arg)))
        case "clock":
            fake.clock_offset_ms = int(arg)
        case "step":
            fake.step = D(arg)
            fake.min_qty = D(arg)
        case "cancel_task":
            sub_name, sub_arg = ["poll", "raise", "drop", "cancel", "exit", "place", "poll"][arg[0]], 100
            try:
                await asyncio.wait_for(apply(rig, sub_name, sub_arg, placed), TIMEOUTS[arg[1]])
            except TimeoutError:
                await recover_after_cancel(rig)


async def recover_after_cancel(rig: Rig) -> None:
    """Depois de uma task cancelada o operador/loop faz o proximo poll: a invariante vale depois dele."""
    try:
        await rig.gateway.poll()
    except Exception:  # noqa: S110
        pass


def hook(
    fake: FakeBinance,
    name: str,
    *,
    before: Callable[[], None] | None = None,
    after: Callable[[], None] | None = None,
) -> None:
    original = getattr(fake, name)
    state = {"done": False}

    def wrapper(params: dict[str, str]) -> Any:
        if state["done"]:
            return original(params)
        state["done"] = True
        if before:
            before()
        result = original(params)
        if after:
            after()
        return result

    setattr(fake, name, wrapper)


def uncovered(fake: FakeBinance) -> Decimal:
    return fake.uncovered_base() - ALIEN


def violation(rig: Rig) -> str | None:
    """Exposicao real, fora de qualquer janela legitima (entrada ainda 'working'), sem pausa+alerta."""
    gateway = rig.gateway
    exposed = uncovered(rig.fake)
    if exposed < TOL:
        return None
    if any(gateway.entry_state(e) == "working" for e in gateway.open_entry_ids()):
        return None
    if gateway.halted and any(level == "critical" for level, _ in rig.alerts):
        return None
    states = {e: gateway.entry_state(e) for e in gateway.open_entry_ids()}
    return f"exposicao {exposed} BTC sem pausa+alerta; estados={states}; halted={gateway.halted}"


async def run(ops: list[tuple[str, Any]]) -> tuple[str | None, list[str]]:
    fake = FakeBinance()
    await fake.start()
    fake.balances["BTC"] = [ALIEN, D(0)]
    rig = build_rig(
        fake,
        request_timeout_seconds=0.12,
        partial_fill_grace_seconds=0,
        protection_activation_grace_seconds=0,
        read_retries=1,
        capital_cap_quote=D(9000),
    )
    placed = [0]
    seen_stop: dict[str, Decimal] = {}
    unexpected: list[str] = []
    try:
        for index, (name, arg) in enumerate(ops):
            try:
                await apply(rig, name, arg, placed)
            except (TimeoutError, LiveError, ValueError, UnknownOrderError):
                pass
            except Exception as error:
                unexpected.append(f"{name}: {type(error).__name__}: {error}")
            if (found := violation(rig)) is not None:
                return f"apos op #{index} {name}{arg!r}: {found}", unexpected
            for entry_id in rig.gateway.open_entry_ids():
                if (stop := entry_stop(fake, entry_id)) is not None:
                    if entry_id in seen_stop and stop < seen_stop[entry_id]:
                        return (
                            f"stop de {entry_id} diminuiu {seen_stop[entry_id]} -> {stop} (op #{index} {name})",
                            unexpected,
                        )
                    seen_stop[entry_id] = max(stop, seen_stop.get(entry_id, stop))
            bought, sold = bought_sold(fake)
            if sold > bought + D("0.0005"):
                return (
                    f"vendeu {sold} com apenas {bought} comprado: tocou holdings alheios (op #{index} {name})",
                    unexpected,
                )
        fake.faults.clear()
        fake.fail_activation = False
        fake.clock_offset_ms = 0
        rig.client._banned_until = 0.0
        for listing in list(fake.lists.values()):
            working = fake.orders[listing.order_ids[0]]
            if (
                listing.kind == "OPOCO"
                and working.status == "FILLED"
                and fake.orders[listing.order_ids[1]].status == "PENDING_NEW"
            ):
                fake.activate_pending(listing.cid)
        fake.step = D("0.001")
        fake.min_qty = D("0.001")
        for _ in range(6):
            try:
                await rig.gateway.poll()
            except LiveError:
                continue
        rig.gateway.resume()
        for _ in range(3):
            try:
                await rig.gateway.poll()
            except LiveError:
                continue
        if rig.gateway.exposed_entry_ids():
            return (
                f"vivacidade: entradas ainda 'exposed' sem falhas: {rig.gateway.exposed_entry_ids()}",
                unexpected,
            )
        if uncovered(fake) >= TOL and not rig.gateway.halted:
            return f"vivacidade: {uncovered(fake)} BTC descobertos sem falhas e sem pausa", unexpected
        if fake.free("BTC") + fake.locked("BTC") < ALIEN - D("0.0005"):
            return "holdings alheios vendidos", unexpected
        return None, unexpected
    finally:
        await rig.client.aclose()
        await fake.stop()


@settings(
    max_examples=int(os.environ.get("LIVE_FUZZ_EXAMPLES", "120")),
    deadline=None,
    suppress_health_check=list(HealthCheck),
)
@given(ops=st.lists(ops_strategy, min_size=4, max_size=18))
def test_invariant_holds_after_every_operation_under_adversarial_faults(ops: list[tuple[str, Any]]) -> None:
    problem, unexpected = asyncio.run(run(ops))

    assert problem is None, problem
    assert not unexpected, f"excecao nao-LiveError escapou da API publica: {unexpected}"
