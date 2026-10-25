"""Sequencias adversariais contra a invariante 'NUNCA posicao sem stop' do LiveGateway (revisao QA 7B-1).

Estes testes nasceram como `xfail(strict=True)` (evidencia de defeitos reais, 7B-1); os defeitos foram
corrigidos no gateway e os xfail removidos: aqui valem como regressao permanente.
A fake local (`FakeBinance`) e a unica contraparte: nenhuma chamada a Binance real/Testnet.
"""

import contextlib
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest

from engine.application.risk import ManagedPosition
from engine.domain.order import ExitOrder, OrderType
from engine.infrastructure.live.config import LiveConfig
from engine.infrastructure.live.errors import FilterViolationError, ForbiddenEndpointError, ReadOnlyModeError
from engine.infrastructure.live.fills import FillKind, FillLog, LiveFill
from engine.infrastructure.live.gateway import derived_id
from tests.fakes.fake_binance import FakeBinance
from tests.unit.live.support import NOW, SYMBOL, Rig, approved_entry, build_rig

D = Decimal


async def protected(rig: Rig, price: str = "103") -> None:
    await rig.gateway.place_order(approved_entry())
    rig.fake.set_price("BTCUSDT", D(99))
    await rig.gateway.poll()
    rig.fake.set_price("BTCUSDT", D(price))
    rig.fake.reset_log()


def naked(rig: Rig) -> bool:
    """Posicao exposta na corretora E gateway nao pausado (sem alerta de pausa)."""
    return rig.fake.uncovered_base() > 0 and not rig.gateway.halted


async def attempt(coro: Any) -> BaseException | None:
    try:
        await coro
    except BaseException as error:
        return error
    return None


# ---- falhas na janela do cancel+replace --------------------------------------------------------------


async def test_account_read_failure_inside_replace_window_does_not_leave_position_naked(rig: Rig) -> None:
    await protected(rig)
    rig.fake.fail("/api/v3/account", times=3, kind="http", status=503)

    await attempt(rig.gateway.raise_stop("e-1", D("97")))

    assert not naked(rig), "posicao exposta e gateway nao pausado"


async def test_oco_post_outage_in_replace_window_escalates_to_emergency_or_halt(rig: Rig) -> None:
    await protected(rig)
    rig.fake.fail("/api/v3/orderList/oco", times=50, kind="http", status=503)

    await attempt(rig.gateway.raise_stop("e-1", D("97")))

    assert not naked(rig), "posicao exposta e gateway nao pausado"


async def test_ip_ban_during_emergency_sale_halts_gateway(rig: Rig) -> None:
    await protected(rig)
    rig.fake.fail("/api/v3/orderList/oco", times=3, kind="binance", status=400, code=-1000)
    rig.fake.fail(
        "/api/v3/order", method="POST", times=9, kind="http", status=418, msg="banned until 99999999999999"
    )

    await attempt(rig.gateway.raise_stop("e-1", D("97")))

    assert not naked(rig), "posicao exposta e gateway nao pausado"


async def test_persistent_429_during_emergency_sale_halts_gateway(rig: Rig) -> None:
    await protected(rig)
    rig.fake.fail("/api/v3/orderList/oco", times=3, kind="binance", status=400, code=-1000)
    rig.fake.fail(
        "/api/v3/order", method="POST", times=99, kind="http", status=429, headers={"Retry-After": "0"}
    )

    await attempt(rig.gateway.raise_stop("e-1", D("97")))

    assert not naked(rig), "posicao exposta e gateway nao pausado"


async def test_clock_drift_in_replace_window_does_not_leave_position_naked(rig: Rig) -> None:
    await protected(rig)
    original = rig.fake._delete_list

    def delete_then_drift(params: dict[str, str]) -> dict[str, Any]:
        result = original(params)
        rig.fake.clock_offset_ms = 7000
        return result

    rig.fake._delete_list = delete_then_drift  # type: ignore[method-assign]

    await attempt(rig.gateway.raise_stop("e-1", D("97")))

    assert not naked(rig), "posicao exposta e gateway nao pausado"


async def test_after_failed_emergency_poll_reprotects_when_network_returns(rig: Rig) -> None:
    await protected(rig)
    rig.fake.fail("/api/v3/orderList/oco", times=9, kind="binance", status=400, code=-1000)
    rig.fake.fail("/api/v3/order", method="POST", times=9, kind="binance", status=400, code=-1000)
    await attempt(rig.gateway.raise_stop("e-1", D("97")))
    assert rig.gateway.halted
    rig.fake.faults.clear()

    for _ in range(3):
        await attempt(rig.gateway.poll())

    assert rig.fake.uncovered_base() == 0, (
        "rede voltou, mas a posicao segue sem stop e ninguem tenta proteger"
    )


async def test_emergency_sale_never_sells_more_than_the_position(rig: Rig) -> None:
    await protected(rig)
    rig.fake.balances["BTC"][0] += D(3)
    rig.fake.fail("/api/v3/orderList/oco", times=3, kind="binance", status=400, code=-1000)

    await attempt(rig.gateway.raise_stop("e-1", D("97")))

    assert rig.fake.free("BTC") == D(3), "vendeu BTC que nao pertencia a posicao"


async def test_emergency_market_sell_partially_filled_leaves_no_naked_remainder(rig: Rig) -> None:
    await protected(rig)
    original = rig.fake._fill

    def partial(order: Any, qty: D, price: D, *, maker: bool) -> None:
        if order.type == "MARKET" and order.side == "SELL":
            half = (qty / 2 // rig.fake.step) * rig.fake.step
            original(order, half, price, maker=maker)
            rig.fake._release(order)
            order.status = "EXPIRED"
            return
        original(order, qty, price, maker=maker)

    rig.fake._fill = partial  # type: ignore[method-assign]
    rig.fake.fail("/api/v3/orderList/oco", times=3, kind="binance", status=400, code=-1000)

    await attempt(rig.gateway.raise_stop("e-1", D("97")))

    assert not naked(rig), "metade da posicao ficou na corretora sem stop e o gateway marcou 'closed'"


async def test_protection_error_on_one_entry_does_not_starve_the_others(rig: Rig) -> None:
    await rig.gateway.place_order(approved_entry(cid="e-1"))
    await rig.gateway.place_order(approved_entry(cid="e-2"))
    rig.fake.set_price("BTCUSDT", D(99))
    await rig.gateway.poll()
    rig.fake.set_price("BTCUSDT", D(103))
    for cid in ("e-1", "e-2"):
        rig.fake._delete_list({"symbol": "BTCUSDT", "listClientOrderId": derived_id(cid, "L0")})
    rig.fake.fail("/api/v3/orderList/oco", times=3, kind="binance", status=400, code=-1000)
    rig.fake.fail("/api/v3/order", method="POST", times=3, kind="binance", status=400, code=-1000)

    await attempt(rig.gateway.poll())

    assert len(rig.fake.active_stops()) == 1, "a segunda entrada nao foi reprotegida no mesmo ciclo"


# ---- comportamentos que ja estao corretos (guardam contra regressao) ---------------------------------


async def test_expired_exchange_info_with_failing_refresh_refuses_entry_without_sending(rig: Rig) -> None:
    rig.gateway._filters._loaded_at = -(10**9)
    rig.fake.fail("/api/v3/exchangeInfo", times=9, kind="http", status=503)

    error = await attempt(rig.gateway.place_order(approved_entry()))

    assert error is not None
    assert rig.fake.writes() == []


async def test_expired_exchange_info_is_refreshed_and_new_min_notional_blocks_entry(rig: Rig) -> None:
    rig.gateway._filters._loaded_at = -(10**9)
    rig.fake.min_notional = D(5000)

    error = await attempt(rig.gateway.place_order(approved_entry()))

    assert isinstance(error, FilterViolationError)
    assert rig.fake.writes() == []


async def test_restarted_gateway_reconcile_pauses_on_unknown_protection(rig: Rig, fake: FakeBinance) -> None:
    await protected(rig)
    restarted = build_rig(fake)
    local = ManagedPosition(
        symbol=SYMBOL,
        entry_price=D(99),
        initial_quantity=D(10),
        quantity=D(10),
        initial_stop=D(95),
        stop=D(95),
        take_profit=D(111),
        highest_price=D(99),
        opened_at=NOW,
    )

    result = await restarted.gateway.reconcile([local])

    assert not result.ok
    assert restarted.gateway.halted
    assert fake.uncovered_base() == 0
    await restarted.client.aclose()


async def test_reconcile_sees_one_of_two_same_symbol_protections_missing(rig: Rig) -> None:
    await rig.gateway.place_order(approved_entry(cid="e-1"))
    await rig.gateway.place_order(approved_entry(cid="e-2"))
    rig.fake.set_price("BTCUSDT", D(99))
    await rig.gateway.poll()
    rig.fake.set_price("BTCUSDT", D(103))
    rig.fake._delete_list({"symbol": "BTCUSDT", "listClientOrderId": derived_id("e-2", "L0")})
    locals_ = [
        ManagedPosition(
            symbol=SYMBOL,
            entry_price=D(99),
            initial_quantity=D(10),
            quantity=D(10),
            initial_stop=D(95),
            stop=D(95),
            take_profit=D(111),
            highest_price=D(99),
            opened_at=NOW,
        )
        for _ in range(2)
    ]

    result = await rig.gateway.reconcile(locals_)

    assert not result.ok


# ---- cliente: base_url e allowlist -------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1.evil.example",
        "http://localhost.evil.example",
        "https://evil.example",
    ],
)
@pytest.mark.parametrize("environment", ["mainnet", "local_fake"])
def test_base_url_is_restricted_to_binance_or_loopback(url: str, environment: str) -> None:
    with pytest.raises(ValueError, match="base_url"):
        LiveConfig(environment=environment, base_url=url, capital_cap_quote=D(100))


async def test_client_refuses_cancel_all_open_orders(rig: Rig) -> None:
    with pytest.raises(ForbiddenEndpointError):
        await rig.client.request("DELETE", "/api/v3/openOrders", {"symbol": "BTCUSDT"}, signed=True)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/v3/order"),
        ("POST", "/api/v3/orderList/oco"),
        ("DELETE", "/api/v3/orderList"),
        ("POST", "/api/v3/userDataStream"),
    ],
)
async def test_read_only_blocks_every_write_before_the_network(
    fake: FakeBinance, method: str, path: str
) -> None:
    rig = build_rig(fake, read_only=True)
    with pytest.raises(ReadOnlyModeError):
        await rig.client.request(method, path, {"symbol": "BTCUSDT"}, signed=True)
    assert fake.log == []
    await rig.client.aclose()


@pytest.mark.parametrize(
    "path", ["/sapi/v1/capital/withdraw/apply", "/fapi/v1/order", "/api/v3/../sapi/v1/x"]
)
async def test_client_refuses_non_spot_paths(rig: Rig, path: str) -> None:
    with pytest.raises(ForbiddenEndpointError):
        await rig.client.request("POST", path, {}, signed=True)
    assert rig.fake.log == []


# ---- saida parcial, expiracao da aprovacao e persistencia de fills -----------------------------------


async def test_account_read_failure_after_partial_exit_does_not_leave_remainder_naked(rig: Rig) -> None:
    await protected(rig)
    rig.fake.fail("/api/v3/account", times=3, kind="http", status=503)
    order = ExitOrder(
        client_order_id="x-1", symbol=SYMBOL, type=OrderType.MARKET, quantity=D(4), created_at=NOW
    )

    await attempt(rig.gateway.place_order(order))

    assert not naked(rig), "6 BTC expostos e gateway nao pausado"


async def test_entry_resting_past_approval_expiry_is_cancelled_by_poll(rig: Rig) -> None:
    approved = approved_entry()
    await rig.gateway.place_order(approved)
    rig.gateway._now = lambda: approved.guards.expires_at + timedelta(minutes=1)  # type: ignore[assignment]

    await rig.gateway.poll()

    assert not any(o.status == "NEW" and o.side == "BUY" for o in rig.fake.orders.values())


def test_fill_sink_failure_does_not_lose_the_fill_forever() -> None:
    delivered: list[int] = []
    calls = {"n": 0}

    def sink(fill: LiveFill) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("banco indisponivel")
        delivered.append(fill.trade_id)

    log = FillLog(sink)
    trade = {
        "id": 7,
        "isBuyer": True,
        "qty": "1",
        "price": "100",
        "quoteQty": "100",
        "commission": "0",
        "commissionAsset": "BNB",
        "isMaker": True,
        "time": 1_700_000_000_000,
    }
    kwargs: dict[str, Any] = {
        "symbol": SYMBOL,
        "entry_id": "e",
        "order_client_id": "e",
        "kind": FillKind.ENTRY,
    }
    with contextlib.suppress(RuntimeError):
        log.record(trade, **kwargs)
    log.record(trade, **kwargs)

    assert delivered == [7]
