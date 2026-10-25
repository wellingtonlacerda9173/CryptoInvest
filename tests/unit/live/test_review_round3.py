# ruff: noqa: E501
"""Correcoes da 2a revisao QA do 7B-1: ledger por entrada (`owed`), orcamento de tempo da protecao,
confirmacao do cancel antes de fechar, exposicao marcada antes do DELETE, alertas. Somente a fake local."""

import asyncio
import time
from decimal import Decimal
from typing import Any

import pytest
import structlog

from engine.domain.order import ExitOrder, OrderType
from engine.infrastructure.live.errors import FilterViolationError, OutcomeUnknownError, ProtectionError
from tests.fakes.fake_binance import FakeBinance
from tests.unit.live.support import NOW, SYMBOL, Rig, approved_entry, build_rig

D = Decimal


async def attempt(coro: Any) -> BaseException | None:
    try:
        await coro
    except BaseException as error:
        return error
    return None


async def protected(rig: Rig, price: str = "103") -> None:
    await rig.gateway.place_order(approved_entry())
    rig.fake.set_price("BTCUSDT", D(99))
    await rig.gateway.poll()
    rig.fake.set_price("BTCUSDT", D(price))
    rig.fake.reset_log()


def market_exit(qty: str, cid: str = "x-1") -> ExitOrder:
    return ExitOrder(
        client_order_id=cid, symbol=SYMBOL, type=OrderType.MARKET, quantity=D(qty), created_at=NOW
    )


def hook_delete(fake: FakeBinance, before: Any) -> None:
    original = fake._delete_list

    def wrapper(params: dict[str, str]) -> Any:
        before()
        return original(params)

    fake._delete_list = wrapper  # type: ignore[method-assign]


# ---- ledger: nenhuma venda alem do que a entrada deve --------------------------------------------------


async def test_exit_with_no_open_entry_is_refused_and_sends_nothing(fake: FakeBinance) -> None:
    fake.balances["BTC"] = [D(5), D(0)]
    rig = build_rig(fake)
    try:
        error = await attempt(rig.gateway.place_order(market_exit("1")))

        assert isinstance(error, FilterViolationError)
        assert fake.writes() == []
        assert fake.free("BTC") == 5
    finally:
        await rig.client.aclose()


async def test_exit_larger_than_what_the_entry_owes_is_refused_even_with_foreign_balance(
    fake: FakeBinance,
) -> None:
    fake.balances["BTC"] = [D(5), D(0)]
    rig = build_rig(fake)
    try:
        await protected(rig)

        error = await attempt(rig.gateway.place_order(market_exit("11")))

        assert isinstance(error, FilterViolationError)
        assert fake.writes() == []
    finally:
        await rig.client.aclose()


async def test_partial_exit_reduces_what_the_entry_owes_for_the_next_emergency(fake: FakeBinance) -> None:
    fake.balances["BTC"] = [D("0.5"), D(0)]
    rig = build_rig(fake)
    try:
        await protected(rig)
        await rig.gateway.place_order(market_exit("4"))
        fake.fail("/api/v3/orderList/oco", times=9, kind="binance", status=400, code=-1000)

        await attempt(rig.gateway.raise_stop("e-1", D("97")))

        assert fake.free("BTC") + fake.locked("BTC") == D("0.5")
    finally:
        await rig.client.aclose()


async def test_stop_filled_while_unobserved_is_settled_by_the_ledger_not_by_free_balance(
    fake: FakeBinance,
) -> None:
    fake.balances["BTC"] = [D("0.5"), D(0)]
    rig = build_rig(fake)
    try:
        await protected(rig)
        fake.set_price("BTCUSDT", D(90))
        fake.fail("/api/v3/orderList", method="GET", times=1, status=503)
        await attempt(rig.gateway.poll())

        await attempt(rig.gateway.poll())

        assert rig.gateway.entry_state("e-1") == "closed"
        assert fake.free("BTC") + fake.locked("BTC") == D("0.5")
    finally:
        await rig.client.aclose()


# ---- cancel da compra parcial: so avanca depois do GET confirmar ------------------------------------------


async def test_unconfirmed_partial_cancel_keeps_entry_working_halts_alerts_and_retries(
    fake: FakeBinance,
) -> None:
    rig = build_rig(fake, partial_fill_grace_seconds=0)
    try:
        fake.fill_fraction = D("0.5")
        fake.set_price("BTCUSDT", D(90))
        fake.fail("/api/v3/orderList", method="DELETE", times=99, status=503)

        error = await attempt(rig.gateway.place_order(approved_entry()))

        assert isinstance(error, ProtectionError)
        assert rig.gateway.entry_state("e-1") == "working"
        assert rig.gateway.halted and any(level == "critical" for level, _ in rig.alerts)

        fake.faults.clear()
        await rig.gateway.poll()

        assert rig.gateway.entry_state("e-1") in {"protected", "closed"}
        assert fake.uncovered_base() == 0
    finally:
        await rig.client.aclose()


async def test_buy_that_fills_after_a_failed_cancel_is_still_handled_and_never_touches_foreign_balance(
    fake: FakeBinance,
) -> None:
    fake.balances["BTC"] = [D("0.5"), D(0)]
    rig = build_rig(fake, partial_fill_grace_seconds=0)
    try:
        fake.fill_fraction = D("0.5")
        fake.set_price("BTCUSDT", D(99))
        fake.fail("/api/v3/orderList", method="DELETE", times=99, status=503)
        await attempt(rig.gateway.place_order(approved_entry()))
        fake.set_price("BTCUSDT", D(96))
        fake.faults.clear()

        await attempt(rig.gateway.poll())
        await attempt(rig.gateway.poll())

        assert rig.gateway.entry_state("e-1") in {"protected", "closed"}
        assert fake.uncovered_base() - D("0.5") < D("0.011")
        assert fake.free("BTC") + fake.locked("BTC") >= D("0.5")
    finally:
        await rig.client.aclose()


# ---- exposicao marcada ANTES do DELETE ----------------------------------------------------------------------


async def test_entry_is_marked_exposed_before_the_protection_delete_goes_out(rig: Rig) -> None:
    await protected(rig)
    seen: list[str] = []
    hook_delete(rig.fake, lambda: seen.append(rig.gateway.entry_state("e-1")))

    await rig.gateway.raise_stop("e-1", D("97"))

    assert seen == ["exposed"]
    assert rig.gateway.entry_state("e-1") == "protected"


async def test_exit_marks_exposed_before_cancelling_protection(rig: Rig) -> None:
    await protected(rig)
    seen: list[str] = []
    hook_delete(rig.fake, lambda: seen.append(rig.gateway.entry_state("e-1")))

    await rig.gateway.place_order(market_exit("4"))

    assert seen == ["exposed"]


# ---- 2xx nao-JSON nunca e sucesso -----------------------------------------------------------------------------


async def test_non_json_2xx_is_an_unknown_outcome_for_reads_and_writes(rig: Rig) -> None:
    rig.fake.fail("/api/v3/account", times=9, status=200, msg="<html>captive portal</html>")

    with pytest.raises(OutcomeUnknownError):
        await rig.client.request("GET", "/api/v3/account", signed=True)


# ---- alerta de relogio compensado ------------------------------------------------------------------------------


async def test_clock_drift_compensated_on_a_critical_call_is_alerted(rig: Rig) -> None:
    rig.fake.clock_offset_ms = 7000
    rig.client._synced_at = None

    await rig.client.request("GET", "/api/v3/account", signed=True, critical=True)

    assert any("relogio" in message for _, message in rig.alerts)


# ---- orcamento de tempo da protecao -----------------------------------------------------------------------------


async def test_protection_budget_sends_the_emergency_sale_within_the_deadline(fake: FakeBinance) -> None:
    rig = build_rig(
        fake,
        protection_deadline_seconds=0.5,
        request_timeout_seconds=0.2,
        read_retries=1,
        protection_retries=10,
    )
    try:
        await protected(rig)
        fake.fail("/api/v3/orderList/oco", times=99, kind="timeout")
        started = time.monotonic()

        await attempt(rig.gateway.raise_stop("e-1", D("97")))
        elapsed = time.monotonic() - started

        assert fake.uncovered_base() == 0 and fake.free("BTC") == 0
        assert rig.gateway.entry_state("e-1") == "closed"
        assert rig.gateway.halted
        assert elapsed < 0.5 + 1.5, elapsed
    finally:
        await rig.client.aclose()


async def test_exposure_start_alerts_before_the_emergency_sale(fake: FakeBinance) -> None:
    fake.fail_activation = True
    rig = build_rig(fake, protection_activation_grace_seconds=0)
    try:
        await rig.gateway.place_order(approved_entry())
        fake.set_price("BTCUSDT", D(99))
        fake.fail("/api/v3/orderList/oco", times=99, kind="binance", status=400, code=-1000)

        await attempt(rig.gateway.poll())

        first = next(message for level, message in rig.alerts if level == "critical")
        assert "SEM PROTECAO" in first and "orcamento" in first
    finally:
        await rig.client.aclose()


async def test_dust_left_after_an_emergency_sale_raises_a_critical_alert(fake: FakeBinance) -> None:
    rig = build_rig(fake)
    try:
        await protected(rig)
        fake.step, fake.min_qty = D(4), D(4)
        await rig.gateway._filters.refresh()
        fake.fail("/api/v3/orderList/oco", times=9, kind="binance", status=400, code=-1000)

        await attempt(rig.gateway.raise_stop("e-1", D("97")))

        assert any(level == "critical" and "poeira" in message for level, message in rig.alerts), rig.alerts
    finally:
        await rig.client.aclose()


# ---- scrub do traceback inesperado ---------------------------------------------------------------------------------


async def test_unexpected_poll_exception_is_logged_scrubbed(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    await protected(rig)

    async def boom(_: Any) -> None:
        raise RuntimeError("falha apiKey=LEAK signature=" + "a" * 64)

    monkeypatch.setattr(rig.gateway, "_advance", boom)
    with structlog.testing.capture_logs() as captured:
        await attempt(rig.gateway.poll())

    assert "LEAK" not in repr(captured) and "a" * 64 not in repr(captured)
    assert any(entry["event"] == "live_poll_unexpected" for entry in captured)


async def test_cancelled_poll_during_the_wait_for_a_leg_never_leaves_the_lock_held(rig: Rig) -> None:
    await protected(rig)
    task = asyncio.ensure_future(rig.gateway.poll())
    await asyncio.sleep(0)
    task.cancel()
    await attempt(task)

    await asyncio.wait_for(rig.gateway.poll(), timeout=5)
