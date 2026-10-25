# ruff: noqa: E501
"""3a revisao QA do 7B-1: provas contra o ledger/_watch/deadline reescritos. Somente a fake local.

Testes `xfail(strict)` com 'BUG:' sao EVIDENCIA de defeito ainda aberto no codigo de producao.
"""

import asyncio
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any

import pytest

from engine.domain.order import ExitOrder, OrderType
from engine.infrastructure.live.errors import FilterViolationError
from tests.fakes.fake_binance import FakeBinance
from tests.unit.live.support import NOW, SYMBOL, Rig, approved_entry, build_rig

D = Decimal


def market_exit(qty: str, cid: str = "x-1") -> ExitOrder:
    return ExitOrder(
        client_order_id=cid, symbol=SYMBOL, type=OrderType.MARKET, quantity=D(qty), created_at=NOW
    )


def slow_request(rig: Rig, method: str, path: str, *, after: bool, delay: float = 30.0) -> None:
    """A chamada (method, path) demora `delay`s; `after=True` processa na fake e SO ENTAO demora (resposta
    perdida por cancelamento/timeout do lado do gateway)."""
    original = rig.client.request

    async def wrapper(m: str, p: str, *args: Any, **kwargs: Any) -> Any:
        if (m, p) != (method, path):
            return await original(m, p, *args, **kwargs)
        if after:
            result = await original(m, p, *args, **kwargs)
            await asyncio.sleep(delay)
            return result
        await asyncio.sleep(delay)
        return await original(m, p, *args, **kwargs)

    rig.client.request = wrapper  # type: ignore[method-assign]


async def settle(coro: Callable[[], Awaitable[Any]]) -> BaseException | None:
    try:
        await coro()
    except BaseException as error:
        return error
    return None


# ---- ledger obsoleto: saida em entrada nao protegida -----------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason="BUG: saida em entrada nao protegida usa ledger obsoleto (sem _sync_ledger) e vende saldo alheio",
)
async def test_exit_before_first_poll_after_target_filled_never_sells_foreign_balance(
    fake: FakeBinance,
) -> None:
    """Alvo da OCO executou antes do 1o poll (estado ainda `working`, ledger sem a venda): a saida nao pode
    vender o saldo alheio de BTC."""
    fake.balances["BTC"] = [D(5), D(0)]
    rig = build_rig(fake)
    try:
        await rig.gateway.place_order(approved_entry())
        fake.set_price("BTCUSDT", D(99))  # compra enche e a OCO ativa
        fake.set_price("BTCUSDT", D(112))  # alvo executa; o gateway ainda nao fez poll
        assert fake.free("BTC") == 5

        await settle(lambda: rig.gateway.place_order(market_exit("5")))

        assert fake.free("BTC") == 5, "saida vendeu saldo ALHEIO (ledger obsoleto: perna ja tinha vendido)"
    finally:
        await rig.client.aclose()


# ---- reserva de capital que vaza -------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True, reason="BUG: place_order cancelado antes do POST deixa entrada fantasma e reserva presa"
)
async def test_cancelled_place_before_any_order_is_sent_does_not_leak_capital_reservation(
    fake: FakeBinance,
) -> None:
    rig = build_rig(fake)
    try:
        gateway = rig.gateway
        before = (await gateway.get_balances())["USDT"].free

        async def stall() -> Decimal:
            await asyncio.sleep(30)
            return D(0)

        gateway._free_quote = stall  # type: ignore[method-assign]
        task = asyncio.ensure_future(gateway.place_order(approved_entry()))
        await asyncio.sleep(0.05)
        task.cancel()
        await settle(lambda: task)
        assert fake.orders == {}, "nenhuma ordem chegou a corretora"

        after = (await gateway.get_balances())["USDT"].free

        assert after == before, "reserva de capital de uma entrada que nunca existiu ficou presa"
        assert gateway.open_entry_ids() == []
    finally:
        await rig.client.aclose()


@pytest.mark.xfail(
    strict=True, reason="BUG: entrada working sem ordem na corretora nunca e encerrada (poll so retorna)"
)
async def test_entry_whose_post_never_reached_the_exchange_is_not_kept_forever(fake: FakeBinance) -> None:
    """Fantasma: entrada registrada, POST nunca vingou e `_entry_exists` respondeu 'desconhecido' => fica
    `working` para sempre (poll so retorna), segurando capital e o client id."""
    rig = build_rig(fake)
    try:
        gateway = rig.gateway
        fake.fail("/api/v3/orderList", method="GET", times=1000, kind="timeout")
        fake.fail("/api/v3/orderList/opoco", method="POST", times=1000, kind="timeout")
        await settle(lambda: gateway.place_order(approved_entry()))
        fake.faults.clear()
        assert fake.orders == {}

        for _ in range(3):
            await gateway.poll()

        assert gateway.open_entry_ids() == [], "entrada fantasma permanece aberta e reservando capital"
    finally:
        await rig.client.aclose()


# ---- posicao orfa apos reinicio -------------------------------------------------------------------------


async def test_after_restart_the_new_gateway_cannot_close_or_protect_the_previous_position(
    fake: FakeBinance,
) -> None:
    """Documenta o contrato (aceito pelo engenheiro) e o RISCO: nao ha caminho no gateway para reconciliar,
    proteger ou vender uma posicao criada por uma instancia anterior."""
    first = build_rig(fake)
    await first.gateway.place_order(approved_entry())
    fake.set_price("BTCUSDT", D(99))
    await first.gateway.poll()
    await first.client.aclose()

    second = build_rig(fake)
    try:
        error = await settle(lambda: second.gateway.place_order(market_exit("10")))
        result = await second.gateway.reconcile([])

        assert isinstance(error, FilterViolationError)
        assert not result.ok  # OCO viva e posicao desconhecidas: pausa permanente ate intervencao manual
        assert second.gateway.halted
        assert second.gateway.open_entry_ids() == []
    finally:
        await second.client.aclose()


# ---- _watch / cancelamento / deadline ------------------------------------------------------------------


async def protected_rig(fake: FakeBinance, **overrides: Any) -> Rig:
    rig = build_rig(fake, **overrides)
    await rig.gateway.place_order(approved_entry())
    fake.set_price("BTCUSDT", D(99))
    await rig.gateway.poll()
    fake.set_price("BTCUSDT", D(103))
    assert rig.gateway.entry_state("e-1") == "protected"
    return rig


async def test_cancelling_raise_stop_mid_replace_propagates_cancel_pauses_and_poll_recovers(
    fake: FakeBinance,
) -> None:
    rig = await protected_rig(fake)
    try:
        slow_request(rig, "POST", "/api/v3/orderList/oco", after=False)
        task = asyncio.ensure_future(rig.gateway.raise_stop("e-1", D(97)))
        await asyncio.sleep(0.1)
        task.cancel()

        error = await settle(lambda: task)

        assert isinstance(error, asyncio.CancelledError) and task.cancelled()
        assert rig.gateway.halted
        assert rig.gateway.entry_state("e-1") == "exposed"
        rig.client.request = type(rig.client).request.__get__(rig.client)  # type: ignore[method-assign]
        await rig.gateway.poll()
        assert rig.gateway.entry_state("e-1") == "protected"
        assert fake.uncovered_base() == 0
    finally:
        await rig.client.aclose()


async def test_deadline_cutting_an_oco_post_already_processed_adopts_it_and_does_not_sell(
    fake: FakeBinance,
) -> None:
    rig = await protected_rig(fake, protection_deadline_seconds=0.3)
    try:
        slow_request(rig, "POST", "/api/v3/orderList/oco", after=True)

        await settle(lambda: rig.gateway.raise_stop("e-1", D(97)))

        market_sells = [r for r in fake.writes() if r.params.get("type") == "MARKET"]
        assert market_sells == [], "OCO ja processada na corretora foi vendida a mercado em vez de adotada"
        assert rig.gateway.entry_state("e-1") == "protected"
        assert fake.uncovered_base() == 0
    finally:
        await rig.client.aclose()


async def test_deadline_cutting_a_post_that_never_arrived_sells_only_what_the_entry_owes(
    fake: FakeBinance,
) -> None:
    fake.balances["BTC"] = [D(5), D(0)]
    rig = await protected_rig(fake, protection_deadline_seconds=0.3)
    try:
        slow_request(rig, "POST", "/api/v3/orderList/oco", after=False)

        await settle(lambda: rig.gateway.raise_stop("e-1", D(97)))

        assert fake.free("BTC") == 5, "venda de emergencia tocou o saldo alheio"
        assert fake.uncovered_base() == 5
    finally:
        await rig.client.aclose()


# ---- ruido / falso pause ------------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True, reason="BUG: alerta critico espurio de SEM PROTECAO quando o alvo ja vendeu tudo"
)
async def test_target_filled_before_first_poll_closes_flat_without_critical_unprotected_alert(
    fake: FakeBinance,
) -> None:
    rig = build_rig(fake)
    try:
        await rig.gateway.place_order(approved_entry())
        fake.set_price("BTCUSDT", D(99))
        fake.set_price("BTCUSDT", D(112))

        await rig.gateway.poll()

        assert rig.gateway.entry_state("e-1") == "closed"
        critical = [m for lvl, m in rig.alerts if lvl == "critical"]
        assert not critical, (
            f"alerta critico de posicao SEM PROTECAO com a posicao ja vendida no alvo: {critical}"
        )
    finally:
        await rig.client.aclose()


@pytest.mark.xfail(
    strict=True, reason="BUG: leitura transitoria na conclusao normal da OCO pausa o gateway (falso positivo)"
)
async def test_transient_read_failures_when_oco_completes_do_not_pause_gateway(fake: FakeBinance) -> None:
    """Alvo executa; as leituras do ledger falham no momento da conclusao. A posicao esta FECHADA na
    corretora: pausar o gateway (exige resume manual) e falso positivo."""
    rig = await protected_rig(fake)
    try:
        fake.set_price("BTCUSDT", D(112))
        fake.fail("/api/v3/order", method="GET", times=6, kind="timeout")

        await settle(rig.gateway.poll)
        fake.faults.clear()
        await rig.gateway.poll()

        assert rig.gateway.entry_state("e-1") == "closed"
        assert not rig.gateway.halted, rig.gateway.halt_reason
    finally:
        await rig.client.aclose()


# ---- comissao em moeda base + emergencia ----------------------------------------------------------------


@pytest.mark.parametrize("alien", ["0", "3"])
async def test_emergency_sells_net_of_base_commission_and_never_the_alien_balance(
    fake: FakeBinance, alien: str
) -> None:
    fake.commission_in_base = True
    fake.fail_activation = True
    fake.balances["BTC"] = [D(alien), D(0)]
    rig = build_rig(fake, protection_retries=1)
    try:
        await rig.gateway.place_order(approved_entry())
        fake.fail("/api/v3/orderList/oco", method="POST", times=50, kind="binance", status=400, code=-1013)
        fake.set_price("BTCUSDT", D(99))

        await settle(rig.gateway.poll)
        fake.faults.clear()
        await settle(rig.gateway.poll)

        assert fake.free("BTC") == D(alien), "sobrou/faltou base: ledger liquido de comissao errado"
        assert fake.uncovered_base() == D(alien)
    finally:
        await rig.client.aclose()


async def test_partial_fills_with_base_commission_protect_exactly_the_net_quantity(fake: FakeBinance) -> None:
    fake.commission_in_base = True
    fake.balances["BTC"] = [D(2), D(0)]
    rig = build_rig(fake, partial_fill_grace_seconds=0)
    try:
        await rig.gateway.place_order(approved_entry())
        fake.fill_fraction = D("0.5")
        fake.set_price("BTCUSDT", D(99))
        await rig.gateway.poll()
        await rig.gateway.poll()

        covered = sum((o.qty - o.executed for o in fake.active_stops()), D(0))
        assert rig.gateway.entry_state("e-1") == "protected"
        assert covered == D("4.995"), covered
        assert fake.free("BTC") == D(2)
    finally:
        await rig.client.aclose()


# ---- mutacao: _sync_ledger antes de reproteger e deadline na fase de leitura -----------------------------


async def _exposed_with_failed_sync(fake: FakeBinance, rig: Rig) -> None:
    """Alvo executa na janela do raise_stop e a leitura do ledger falha: entrada `exposed`, ledger obsoleto."""
    original = fake._delete_list

    def wrapper(params: dict[str, str]) -> Any:
        fake.set_price("BTCUSDT", D(112))
        return original(params)

    fake._delete_list = wrapper  # type: ignore[method-assign]
    fake.fail("/api/v3/order", method="GET", times=6, kind="timeout")
    await settle(lambda: rig.gateway.raise_stop("e-1", D(100)))
    fake.faults.clear()
    fake._delete_list = original  # type: ignore[method-assign]


async def test_resume_syncs_ledger_before_reprotecting_and_never_protects_foreign_balance(
    fake: FakeBinance,
) -> None:
    fake.balances["BTC"] = [D(5), D(0)]
    rig = await protected_rig(fake, protection_deadline_seconds=120)
    try:
        await _exposed_with_failed_sync(fake, rig)
        assert rig.gateway.entry_state("e-1") == "exposed"
        assert fake.free("BTC") == 5  # alvo vendeu tudo; so o saldo alheio resta

        await settle(rig.gateway.poll)

        assert fake.locked("BTC") == 0, "OCO colocada sobre saldo ALHEIO (ledger obsoleto na retomada)"
        assert rig.gateway.entry_state("e-1") == "closed"
    finally:
        await rig.client.aclose()


async def test_position_exposed_beyond_the_deadline_goes_straight_to_the_emergency_sale(
    fake: FakeBinance,
) -> None:
    rig = await protected_rig(fake, protection_deadline_seconds=0.3)
    try:
        fake.fail("/api/v3/orderList/oco", method="POST", times=60, kind="timeout")
        fake.fail("/api/v3/order", method="POST", times=60, kind="timeout")
        await settle(lambda: rig.gateway.raise_stop("e-1", D(97)))
        assert rig.gateway.entry_state("e-1") == "exposed"
        fake.faults.clear()
        fake.reset_log()

        await asyncio.wait_for(settle(rig.gateway.poll), 5)

        assert [r.params.get("type") for r in fake.writes()] == ["MARKET"], fake.writes()
        assert rig.gateway.entry_state("e-1") == "closed"
    finally:
        await rig.client.aclose()
