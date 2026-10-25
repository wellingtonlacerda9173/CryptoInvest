# ruff: noqa: E501, PT011
"""Revisao QA 7B-1, 2a rodada: defeitos NOVOS da reescrita do LiveGateway (evidencia) e regressoes.

Cada teste aqui reproduz um defeito achado pelo QA e hoje corrigido. Somente a fake local (`FakeBinance`) e
usada: nenhuma chamada a Binance real/Testnet.
"""

import asyncio
import contextlib
from decimal import Decimal
from typing import Any

import pytest

from engine.infrastructure.live.config import LiveConfig
from engine.infrastructure.live.errors import InsufficientBalanceError, LiveError
from tests.fakes.fake_binance import FakeBinance
from tests.unit.live.support import SYMBOL, Rig, approved_entry, build_rig

D = Decimal


def naked(rig: Rig) -> bool:
    """Base exposta na corretora E gateway nao pausado."""
    return rig.fake.uncovered_base() >= D("0.01") and not rig.gateway.halted


async def attempt(coro: Any) -> BaseException | None:
    try:
        await coro
    except BaseException as error:
        return error
    return None


async def until(predicate: Any, *, tries: int = 400) -> None:
    for _ in range(tries):
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condicao nunca ocorreu")


def count(fake: FakeBinance, method: str, path: str) -> int:
    return sum(1 for r in fake.log if r.method == method and r.path == path)


async def protected(rig: Rig, price: str = "103") -> None:
    await rig.gateway.place_order(approved_entry())
    rig.fake.set_price("BTCUSDT", D(99))
    await rig.gateway.poll()
    rig.fake.set_price("BTCUSDT", D(price))
    rig.fake.reset_log()


# ---- cancelamento da task (CancelledError) na janela cancel -> replace ---------------------------------


async def test_task_cancelled_while_cancel_response_in_flight_halts_gateway(rig: Rig) -> None:
    await protected(rig)
    task = asyncio.ensure_future(rig.gateway.raise_stop("e-1", D("97")))
    await until(lambda: count(rig.fake, "DELETE", "/api/v3/orderList") >= 1)
    task.cancel()
    await attempt(task)

    assert rig.fake.uncovered_base() > 0, "pre-condicao: o cancel foi processado na corretora"
    assert not naked(rig), "posicao exposta e gateway nao pausado/alertado"


async def test_after_cancelled_replace_the_next_poll_reprotects_with_critical_alert(rig: Rig) -> None:
    await protected(rig)
    task = asyncio.ensure_future(rig.gateway.raise_stop("e-1", D("97")))
    await until(lambda: count(rig.fake, "DELETE", "/api/v3/orderList") >= 1)
    task.cancel()
    await attempt(task)

    await rig.gateway.poll()

    assert rig.fake.uncovered_base() == 0
    assert rig.gateway.entry_state("e-1") == "protected"
    assert any(level == "critical" for level, _ in rig.alerts)


async def test_task_cancelled_inside_rescue_before_arm_halts_gateway(rig: Rig) -> None:
    rig.fake.fail_activation = True
    await rig.gateway.place_order(approved_entry())
    rig.fake.set_price("BTCUSDT", D(99))
    rig.fake.fail("/api/v3/orderList", method="DELETE", times=1, kind="timeout")
    task = asyncio.ensure_future(rig.gateway.poll())
    await until(lambda: count(rig.fake, "DELETE", "/api/v3/orderList") >= 1)
    task.cancel()
    await attempt(task)

    assert rig.gateway.entry_state("e-1") == "exposed"
    assert not naked(rig), "posicao exposta e gateway nao pausado"


# ---- excecao de LiveError escapando de cancel_order com a posicao exposta ---------------------------------


async def test_cancel_order_with_account_failure_after_partial_fill_halts_gateway(rig: Rig) -> None:
    await rig.gateway.place_order(approved_entry())
    rig.fake.fill_fraction = D("0.5")
    rig.fake.set_price("BTCUSDT", D(99))
    rig.fake.fail("/api/v3/account", times=20, status=503)

    error = await attempt(rig.gateway.cancel_order(SYMBOL, "e-1"))

    assert isinstance(error, LiveError)
    assert rig.gateway.entry_state("e-1") == "exposed"
    assert not naked(rig), "posicao exposta e gateway nao pausado"


async def test_after_cancel_order_failure_the_next_poll_reprotects(rig: Rig) -> None:
    await rig.gateway.place_order(approved_entry())
    rig.fake.fill_fraction = D("0.5")
    rig.fake.set_price("BTCUSDT", D(99))
    rig.fake.fail("/api/v3/account", times=20, status=503)
    await attempt(rig.gateway.cancel_order(SYMBOL, "e-1"))
    rig.fake.faults.clear()

    await rig.gateway.poll()

    assert rig.fake.uncovered_base() == 0
    assert rig.gateway.entry_state("e-1") == "protected"


# ---- corrida de capital: duas entradas concorrentes -------------------------------------------------------


async def test_two_concurrent_entries_cannot_exceed_the_capital_cap(fake: FakeBinance) -> None:
    first = approved_entry(cid="e-1")
    cost = first.order.quantity * first.order.price
    rig = build_rig(fake, capital_cap_quote=cost * D("1.5"))
    try:
        results = await asyncio.gather(
            rig.gateway.place_order(first),
            rig.gateway.place_order(approved_entry(cid="e-2")),
            return_exceptions=True,
        )
    finally:
        await rig.client.aclose()

    assert any(isinstance(r, InsufficientBalanceError) for r in results), results


# ---- config: caracteres de controle no base_url ---------------------------------------------------------


@pytest.mark.parametrize(
    "url", ["https://api.binance.com\n", "https://api.bin\tance.com", "https://api.binance.com/\r"]
)
def test_base_url_with_control_characters_is_rejected(url: str) -> None:
    with pytest.raises(ValueError):
        LiveConfig(environment="mainnet", base_url=url, capital_cap_quote=D(100))


# ---- venda de emergencia: MARKET_LOT_SIZE preserva a quantidade total --------------------------------------


@pytest.mark.parametrize(("max_qty", "step"), [("7", "0.001"), ("3.3", "0.1"), ("20", "0.5")])
async def test_emergency_chunks_sell_the_whole_position_up_to_dust(
    fake: FakeBinance, max_qty: str, step: str
) -> None:
    fake.market_max_qty, fake.market_step = D(max_qty), D(step)
    rig = build_rig(fake)
    try:
        await protected(rig)
        quantity = rig.gateway.protection_of("e-1")[0]  # type: ignore[index]
        rig.fake.fail("/api/v3/orderList/oco", times=9, kind="binance", status=400, code=-1000)

        await attempt(rig.gateway.raise_stop("e-1", D("97")))

        leftover = fake.free("BTC") + fake.locked("BTC")
        assert leftover < D(step) or leftover < D("0.001"), (quantity, leftover)
        assert fake.uncovered_base() < D(step)
    finally:
        await rig.client.aclose()


async def test_emergency_never_touches_holdings_that_belong_to_someone_else(fake: FakeBinance) -> None:
    fake.balances["BTC"] = [D("0.5"), D(0)]
    rig = build_rig(fake)
    try:
        await protected(rig)
        rig.fake.fail("/api/v3/orderList/oco", times=9, kind="binance", status=400, code=-1000)

        await attempt(rig.gateway.raise_stop("e-1", D("97")))

        assert fake.free("BTC") + fake.locked("BTC") == D("0.5")
    finally:
        await rig.client.aclose()


async def test_cancelled_task_never_leaves_the_lock_held(rig: Rig) -> None:
    await protected(rig)
    task = asyncio.ensure_future(rig.gateway.raise_stop("e-1", D("97")))
    await until(lambda: count(rig.fake, "DELETE", "/api/v3/orderList") >= 1)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    await asyncio.wait_for(rig.gateway.poll(), timeout=5)


# ---- stop/alvo executado ANTES do primeiro poll: _confirm_or_rescue ignora pernas executadas -----------------


@pytest.mark.parametrize("final_price", ["90", "115"])
async def test_leg_executed_before_first_poll_never_sells_someone_elses_balance(
    fake: FakeBinance, final_price: str
) -> None:
    fake.balances["BTC"] = [D("0.5"), D(0)]
    rig = build_rig(fake)
    try:
        await rig.gateway.place_order(approved_entry())
        fake.set_price("BTCUSDT", D(99))
        fake.set_price("BTCUSDT", D(final_price))

        await attempt(rig.gateway.poll())

        assert fake.free("BTC") + fake.locked("BTC") == D("0.5")
    finally:
        await rig.client.aclose()


@pytest.mark.parametrize("final_price", ["90", "115"])
async def test_leg_executed_before_first_poll_without_foreign_balance_ends_closed_and_flat(
    fake: FakeBinance, final_price: str
) -> None:
    rig = build_rig(fake)
    try:
        await rig.gateway.place_order(approved_entry())
        fake.set_price("BTCUSDT", D(99))
        fake.set_price("BTCUSDT", D(final_price))

        await attempt(rig.gateway.poll())

        assert rig.gateway.entry_state("e-1") == "closed"
        assert fake.free("BTC") + fake.locked("BTC") == 0
    finally:
        await rig.client.aclose()


async def test_exit_on_unprotected_entry_is_deducted_from_what_the_entry_owes(fake: FakeBinance) -> None:
    from engine.domain.order import ExitOrder, OrderType
    from tests.unit.live.support import NOW

    fake.balances["BTC"] = [D("0.5"), D(0)]
    rig = build_rig(fake)
    try:
        await rig.gateway.place_order(approved_entry())
        fake.set_price("BTCUSDT", D(90))
        await rig.gateway.place_order(
            ExitOrder(
                client_order_id="x-1", symbol=SYMBOL, type=OrderType.MARKET, quantity=D(1), created_at=NOW
            )
        )

        await attempt(rig.gateway.poll())

        assert fake.free("BTC") + fake.locked("BTC") == D("0.5")
    finally:
        await rig.client.aclose()


async def test_operator_resume_does_not_release_new_entries_while_a_position_is_exposed(rig: Rig) -> None:
    from engine.infrastructure.live.errors import TradingHaltedError

    await protected(rig)
    rig.fake.fail("/api/v3/orderList/oco", times=99, kind="binance", status=400, code=-1000)
    rig.fake.fail("/api/v3/order", method="POST", times=99, kind="binance", status=400, code=-1000)
    await attempt(rig.gateway.raise_stop("e-1", D("97")))
    assert rig.gateway.halted and rig.gateway.exposed_entry_ids() == ["e-1"]

    rig.gateway.resume()
    error = await attempt(rig.gateway.place_order(approved_entry(cid="e-2")))

    assert isinstance(error, TradingHaltedError)
    assert count(rig.fake, "POST", "/api/v3/orderList/opoco") == 0


async def test_non_json_200_on_the_replacement_oco_is_not_taken_as_success(rig: Rig) -> None:
    await protected(rig)
    rig.fake.fail("/api/v3/orderList/oco", times=1, status=200, msg="<html>proxy</html>")

    await attempt(rig.gateway.raise_stop("e-1", D("97")))

    assert rig.fake.uncovered_base() == 0, "gateway acredita estar protegido, corretora sem stop"


async def test_unconfirmable_cancel_that_actually_happened_halts_instead_of_claiming_protection(
    rig: Rig,
) -> None:
    await protected(rig)
    rig.fake.fail(
        "/api/v3/orderList", method="DELETE", times=2, after=True
    )  # aiohttp reenvia DELETE (idempotente) 1x
    rig.fake.fail("/api/v3/orderList", method="GET", times=20, status=503)

    error = await attempt(rig.gateway.raise_stop("e-1", D("97")))

    assert rig.fake.uncovered_base() > 0, "pre-condicao: a corretora ja cancelou"
    assert error is not None
    assert not naked(rig), "gateway diz 'protecao mantida' mas a posicao esta sem stop e sem pausa"


async def test_secrets_and_signatures_never_leak_under_a_chaotic_run(rig: Rig) -> None:
    import re
    import traceback

    import structlog

    from tests.fakes.fake_binance import API_KEY, API_SECRET

    texts: list[str] = []
    with structlog.testing.capture_logs() as captured:
        await protected(rig)
        for path, method, kind in (
            ("/api/v3/orderList/oco", "POST", "binance"),
            ("/api/v3/account", "GET", "http"),
            ("/api/v3/order", "GET", "timeout"),
            ("/api/v3/order", "POST", "http"),
            ("/api/v3/orderList", "DELETE", "http"),
        ):
            if kind == "binance":
                rig.fake.fail(
                    path,
                    method=method,
                    times=2,
                    kind="binance",
                    status=400,
                    code=-1000,
                    msg="bad request apiKey=LEAK signature=" + "b" * 64,
                )
            elif kind == "timeout":
                rig.fake.fail(path, method=method, times=1, kind="timeout")
            else:
                rig.fake.fail(
                    path,
                    method=method,
                    times=2,
                    status=503,
                    msg="upstream api_key=LEAK&signature=" + "a" * 64,
                )
        for op in (
            lambda: rig.gateway.raise_stop("e-1", D("97")),
            lambda: rig.gateway.poll(),
            lambda: rig.gateway.poll(),
        ):
            error = await attempt(op())
            if error is not None:
                texts.append("".join(traceback.format_exception(error)) + repr(error) + str(error))
    rig.fake.faults.clear()
    rig.fake.fail(
        "/api/v3/account", kind="binance", status=400, code=-1000, msg="bad apiKey=LEAK signature=" + "c" * 64
    )
    error = await attempt(rig.gateway.get_balances())
    assert error is not None
    texts.append("".join(traceback.format_exception(error)) + repr(error) + str(error))
    blob = "\n".join(texts) + repr(captured) + repr(rig.alerts) + repr(rig.gateway.halt_reason)

    assert API_SECRET not in blob and API_KEY not in blob
    assert not re.search(r"signature=[0-9a-f]{64}", blob), "assinatura HMAC vazou"
    assert "LEAK" not in blob, "corpo de erro do servidor vazou sem scrub"


async def test_persistent_poll_failure_on_a_protected_entry_raises_an_alert(rig: Rig) -> None:
    await protected(rig)
    rig.fake.clock_offset_ms = 7000
    rig.client._synced_at = None

    for _ in range(5):
        await attempt(rig.gateway.poll())

    assert rig.gateway.entry_state("e-1") == "protected"
    assert rig.alerts or rig.gateway.halted, "5 polls seguidos falharam e ninguem foi avisado"


async def test_partial_entry_whose_cancel_failed_is_never_closed_while_the_buy_order_is_alive(
    fake: FakeBinance,
) -> None:
    rig = build_rig(fake, partial_fill_grace_seconds=0)
    try:
        fake.fill_fraction = D("0.5")
        fake.set_price("BTCUSDT", D(90))
        fake.fail("/api/v3/orderList", method="DELETE", times=2, status=503)
        await attempt(rig.gateway.place_order(approved_entry()))
        await attempt(rig.gateway.poll())
        fake.set_price("BTCUSDT", D(90))

        await attempt(rig.gateway.poll())

        assert not naked(rig), (
            "fill posterior da compra nao cancelada ficou sem stop, sem controle e sem pausa"
        )
    finally:
        await rig.client.aclose()
