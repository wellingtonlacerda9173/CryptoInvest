"""Retomada segura, venda de emergencia, expiracao, MARKET_LOT_SIZE, comissao e outbox de fills."""

import time
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest

from engine.domain.order import ExitOrder, OrderType
from engine.infrastructure.live.errors import FilterViolationError, ProtectionError, TradingHaltedError
from engine.infrastructure.live.fills import FillKind, FillLog, LiveFill
from tests.fakes.fake_binance import ApiError, FakeBinance
from tests.unit.live.support import NOW, SYMBOL, Rig, approved_entry, build_rig

D = Decimal


async def protected(rig: Rig, price: str = "103") -> None:
    await rig.gateway.place_order(approved_entry())
    rig.fake.set_price("BTCUSDT", D(99))
    await rig.gateway.poll()
    rig.fake.set_price("BTCUSDT", D(price))
    rig.fake.reset_log()


async def attempt(coro: Any) -> BaseException | None:
    try:
        await coro
    except BaseException as error:
        return error
    return None


def break_everything(fake: FakeBinance) -> None:
    fake.fail("/api/v3/orderList/oco", times=99, kind="binance", status=400, code=-1000)
    fake.fail("/api/v3/order", method="POST", times=99, kind="binance", status=400, code=-1000)


# ---- retomada segura -----------------------------------------------------------------------------------


async def test_exposed_entry_is_flagged_halted_alerted_and_blocks_new_entries(rig: Rig) -> None:
    await protected(rig)
    break_everything(rig.fake)

    error = await attempt(rig.gateway.raise_stop("e-1", D("97")))

    assert isinstance(error, ProtectionError)
    assert rig.gateway.entry_state("e-1") == "exposed"
    assert rig.gateway.halted
    assert rig.gateway.exposed_entry_ids() == ["e-1"]
    assert any(level == "critical" and "PAUSADO" in msg for level, msg in rig.alerts)
    rig.gateway.resume()
    rig.fake.reset_log()
    with pytest.raises(TradingHaltedError, match="SEM PROTECAO"):
        await rig.gateway.place_order(approved_entry(cid="e-2"))
    assert not any(r.path.endswith("/opoco") for r in rig.fake.log)


async def test_after_ban_lapses_the_first_action_is_to_reprotect_before_any_new_entry(rig: Rig) -> None:
    await protected(rig)
    rig.fake.fail("/api/v3/orderList/oco", times=3, kind="binance", status=400, code=-1000)
    rig.fake.fail("/api/v3/order", method="POST", times=9, status=418, msg="banned until 99999999999999")
    await attempt(rig.gateway.raise_stop("e-1", D("97")))
    assert rig.gateway.halted
    assert rig.gateway.entry_state("e-1") == "exposed"
    assert rig.fake.uncovered_base() > 0
    rig.fake.faults.clear()
    rig.client._banned_until = 0.0
    rig.fake.reset_log()

    rig.gateway.resume()
    result = await attempt(rig.gateway.place_order(approved_entry(cid="e-2")))

    assert result is None
    assert rig.fake.uncovered_base() == 0
    first_write = rig.fake.writes()[0]
    assert (first_write.method, first_write.path) == ("POST", "/api/v3/orderList/oco")
    assert rig.gateway.entry_state("e-1") == "protected"
    assert rig.gateway.entry_state("e-2") == "working"


async def test_banned_ip_halts_without_hammering_the_exchange(rig: Rig) -> None:
    await protected(rig)
    rig.fake.fail("/api/v3/orderList/oco", times=3, kind="binance", status=400, code=-1000)
    rig.fake.fail("/api/v3/order", method="POST", times=9, status=418, msg="banned until 99999999999999")
    await attempt(rig.gateway.raise_stop("e-1", D("97")))
    rig.fake.reset_log()

    for _ in range(3):
        await attempt(rig.gateway.poll())

    assert rig.fake.log == []


async def test_poll_adopts_a_protection_created_by_an_unconfirmed_post(rig: Rig) -> None:
    await protected(rig)
    rig.fake.fail("/api/v3/orderList/oco", times=1, after=True)
    rig.fake.fail("/api/v3/orderList", method="GET", times=99, status=503)

    await attempt(rig.gateway.raise_stop("e-1", D("97")))
    rig.fake.faults.clear()
    await attempt(rig.gateway.poll())

    assert rig.fake.uncovered_base() == 0
    assert len(rig.fake.active_stops()) == 1
    assert rig.gateway.entry_state("e-1") == "protected"


# ---- venda de emergencia ---------------------------------------------------------------------------------


async def test_emergency_sale_splits_by_market_lot_size_max_qty(rig: Rig) -> None:
    rig.fake.market_max_qty = D(4)
    await protected(rig)
    rig.fake.fail("/api/v3/orderList/oco", times=3, kind="binance", status=400, code=-1000)

    await rig.gateway.raise_stop("e-1", D("97"))

    sells = [r for r in rig.fake.writes() if r.method == "POST" and r.path == "/api/v3/order"]
    assert [D(r.params["quantity"]) for r in sells] == [D(4), D(4), D(2)]
    assert rig.fake.uncovered_base() == 0
    assert rig.gateway.entry_state("e-1") == "closed"
    assert not rig.gateway.halted


async def test_emergency_sale_respects_market_step_coarser_than_lot_step(rig: Rig) -> None:
    rig.fake.market_step = D("0.5")
    await protected(rig)
    rig.fake.balances["BTC"][0] += D("0.3")
    rig.fake.fail("/api/v3/orderList/oco", times=3, kind="binance", status=400, code=-1000)

    await rig.gateway.raise_stop("e-1", D("97"))

    assert rig.fake.free("BTC") == D("0.3")
    assert rig.gateway.entry_state("e-1") == "closed"


async def test_caller_market_exit_outside_market_lot_size_is_refused_before_sending(rig: Rig) -> None:
    rig.fake.market_max_qty = D(4)
    await protected(rig)
    order = ExitOrder(
        client_order_id="x-9", symbol=SYMBOL, type=OrderType.MARKET, quantity=D(5), created_at=NOW
    )

    with pytest.raises(FilterViolationError, match="MARKET_LOT_SIZE"):
        await rig.gateway.place_order(order)

    assert rig.fake.writes() == []


async def test_emergency_sale_that_never_executes_stays_exposed_and_halts(rig: Rig) -> None:
    await protected(rig)
    original = rig.fake._fill

    def sell_nothing(order: Any, qty: D, price: D, *, maker: bool) -> None:
        if order.type == "MARKET" and order.side == "SELL":
            rig.fake._release(order)
            order.status = "EXPIRED"
            return
        original(order, qty, price, maker=maker)

    rig.fake._fill = sell_nothing  # type: ignore[method-assign]
    rig.fake.fail("/api/v3/orderList/oco", times=3, kind="binance", status=400, code=-1000)

    error = await attempt(rig.gateway.raise_stop("e-1", D("97")))

    assert isinstance(error, ProtectionError)
    assert rig.gateway.entry_state("e-1") == "exposed"
    assert rig.gateway.halted
    sells = [r for r in rig.fake.writes() if r.method == "POST" and r.path == "/api/v3/order"]
    assert len(sells) == rig.gateway._config.emergency_max_attempts


# ---- aprovacao vencida -----------------------------------------------------------------------------------


async def test_expiry_cancel_loses_race_to_a_fill_and_the_fill_is_protected_normally(rig: Rig) -> None:
    approved = approved_entry()
    await rig.gateway.place_order(approved)

    def fill_then_refuse(params: dict[str, str]) -> dict[str, Any]:
        rig.fake.set_price("BTCUSDT", D(99))
        raise ApiError(400, -2011, "Unknown order sent.")

    rig.fake._delete_list = fill_then_refuse  # type: ignore[method-assign]
    rig.gateway._now = lambda: approved.guards.expires_at + timedelta(seconds=1)  # type: ignore[assignment]

    await rig.gateway.poll()

    assert rig.gateway.entry_state("e-1") == "protected"
    assert rig.fake.uncovered_base() == 0
    assert not any(r.path == "/api/v3/order" and r.method == "POST" for r in rig.fake.log)


async def test_expiry_after_partial_fill_protects_only_what_executed(rig: Rig) -> None:
    approved = approved_entry()
    rig.fake.fill_fraction = D("0.5")
    await rig.gateway.place_order(approved)
    rig.fake.set_price("BTCUSDT", D(99))
    rig.gateway._now = lambda: approved.guards.expires_at + timedelta(seconds=1)  # type: ignore[assignment]

    await rig.gateway.poll()

    assert rig.gateway.entry_state("e-1") == "protected"
    assert rig.fake.uncovered_base() == 0
    resting = [
        o for o in rig.fake.orders.values() if o.status in ("NEW", "PARTIALLY_FILLED") and o.side == "BUY"
    ]
    assert resting == []


async def test_unexpired_entry_is_left_resting(rig: Rig) -> None:
    await rig.gateway.place_order(approved_entry())

    await rig.gateway.poll()

    assert any(o.status == "NEW" and o.side == "BUY" for o in rig.fake.orders.values())


# ---- comissao ---------------------------------------------------------------------------------------------


async def test_bnb_commission_is_converted_to_quote_at_the_moment_rate(rig: Rig) -> None:
    await rig.gateway.place_order(approved_entry())
    rig.fake.set_price("BTCUSDT", D(99))
    await rig.gateway.poll()

    fill = rig.gateway.fills()[0]

    assert fill.commission_asset == "BNB"
    assert not fill.commission_estimated
    assert fill.commission_quote == fill.commission * 600


async def test_bnb_commission_without_a_rate_uses_a_conservative_estimate_flagged_as_such(rig: Rig) -> None:
    rig.fake.aux_prices.clear()
    await rig.gateway.place_order(approved_entry())
    rig.fake.set_price("BTCUSDT", D(99))
    await rig.gateway.poll()

    fill = rig.gateway.fills()[0]

    assert fill.commission_asset == "BNB"
    assert fill.commission_estimated
    assert fill.commission_quote == fill.quote_quantity * D("0.001")
    assert rig.gateway.entry_state("e-1") == "protected"


# ---- outbox de fills ---------------------------------------------------------------------------------------

TRADE = {
    "id": 7,
    "isBuyer": True,
    "qty": "1",
    "price": "100",
    "quoteQty": "100",
    "commission": "0.1",
    "commissionAsset": "USDT",
    "isMaker": True,
    "time": 1_700_000_000_000,
}
KW: dict[str, Any] = {"symbol": SYMBOL, "entry_id": "e", "order_client_id": "e", "kind": FillKind.ENTRY}


def test_sink_outage_keeps_fills_in_outbox_and_delivers_each_exactly_once_in_order() -> None:
    delivered: list[int] = []
    down = [True]

    def sink(fill: LiveFill) -> None:
        if down[0]:
            raise RuntimeError("banco indisponivel")
        delivered.append(fill.trade_id)

    log = FillLog(sink)
    log.record(TRADE, **KW)
    log.record({**TRADE, "id": 8}, **KW)
    assert log.pending == 2
    assert len(log.all()) == 2
    down[0] = False

    assert log.flush()
    log.record(TRADE, **KW)
    log.flush()

    assert delivered == [7, 8]
    assert log.pending == 0


def test_failing_sink_is_reported_and_never_raises_into_the_protection_flow() -> None:
    reported: list[int] = []

    def sink(fill: LiveFill) -> None:
        raise RuntimeError("down")

    log = FillLog(sink, reported.append)

    assert log.record(TRADE, **KW) is not None
    assert reported == [1]


async def test_gateway_survives_sink_failure_and_redelivers_on_poll(fake: FakeBinance) -> None:
    rig = build_rig(fake)
    delivered: list[int] = []
    state = {"down": True}

    def sink(fill: LiveFill) -> None:
        if state["down"]:
            raise RuntimeError("banco indisponivel")
        delivered.append(fill.trade_id)

    rig.gateway._fills = FillLog(sink)
    await rig.gateway.place_order(approved_entry())
    fake.set_price("BTCUSDT", D(99))
    await rig.gateway.poll()
    assert rig.gateway.entry_state("e-1") == "protected"
    assert delivered == []
    state["down"] = False

    await rig.gateway.poll()

    assert delivered == [1]
    await rig.client.aclose()


# ---- janela de exposicao ---------------------------------------------------------------------------------


async def test_exposure_windows_are_minimal_and_measured_on_the_fake(rig: Rig) -> None:
    started = time.monotonic()
    await rig.gateway.place_order(approved_entry())
    rig.fake.set_price("BTCUSDT", D(99))
    await rig.gateway.poll()
    assert rig.fake.max_exposure() == (0.0, 0), "OPOCO ativa as pernas no proprio fill: nenhuma janela"

    rig.fake.set_price("BTCUSDT", D(103))
    await rig.gateway.raise_stop("e-1", D("97"))
    seconds, requests = rig.fake.max_exposure()
    assert requests == 1, "cancel+replace: unica requisicao dentro da janela e o POST do novo OCO"
    assert seconds < 1.0
    assert time.monotonic() - started < 10


async def test_exposure_window_after_partial_fill_is_bounded_by_the_rescue_requests(
    fake: FakeBinance,
) -> None:
    rig = build_rig(fake, partial_fill_grace_seconds=0)
    fake.fill_fraction = D("0.5")
    await rig.gateway.place_order(approved_entry())
    fake.set_price("BTCUSDT", D(99))
    await rig.gateway.poll()

    seconds, requests = fake.max_exposure()
    assert fake.uncovered_base() == 0
    assert requests <= 12
    assert seconds < 1.0
    await rig.client.aclose()


async def test_exposure_window_when_opoco_legs_do_not_activate_is_bounded_by_the_grace(
    fake: FakeBinance,
) -> None:
    rig = build_rig(fake, protection_activation_grace_seconds=0)
    fake.fail_activation = True
    await rig.gateway.place_order(approved_entry())
    fake.set_price("BTCUSDT", D(99))
    await rig.gateway.poll()

    seconds, requests = fake.max_exposure()
    assert fake.uncovered_base() == 0
    assert requests <= 12
    assert seconds < 1.0
    await rig.client.aclose()


async def test_logical_exposure_after_partial_fill_never_exceeds_grace_plus_one_poll_interval(
    fake: FakeBinance,
) -> None:
    rig = build_rig(fake)
    config = rig.gateway._config
    clock = [1000.0]
    rig.gateway._mono = lambda: clock[0]
    fake.fill_fraction = D("0.5")
    await rig.gateway.place_order(approved_entry())
    fake.set_price("BTCUSDT", D(99))
    filled_at = clock[0]

    while fake.uncovered_base() > 0:
        await rig.gateway.poll()
        if fake.uncovered_base() > 0:
            clock[0] += config.poll_interval_seconds
        assert clock[0] - filled_at < 60, "exposicao sem fim"

    assert clock[0] - filled_at <= config.partial_fill_grace_seconds + config.poll_interval_seconds
    await rig.client.aclose()


async def test_logical_exposure_when_legs_stay_pending_never_exceeds_grace_plus_one_poll_interval(
    fake: FakeBinance,
) -> None:
    rig = build_rig(fake)
    config = rig.gateway._config
    clock = [1000.0]
    rig.gateway._mono = lambda: clock[0]
    fake.hold_activation = True
    await rig.gateway.place_order(approved_entry())
    fake.set_price("BTCUSDT", D(99))
    filled_at = clock[0]

    while fake.uncovered_base() > 0:
        await rig.gateway.poll()
        if fake.uncovered_base() > 0:
            clock[0] += config.poll_interval_seconds
        assert clock[0] - filled_at < 60, "exposicao sem fim"

    assert clock[0] - filled_at <= config.protection_activation_grace_seconds + config.poll_interval_seconds
    await rig.client.aclose()


# ---- qualquer excecao na janela cancel -> novo OCO -------------------------------------------------------


async def test_persistent_429_on_the_replacement_oco_falls_back_to_emergency_sale(rig: Rig) -> None:
    await protected(rig)
    rig.fake.fail("/api/v3/orderList/oco", times=99, status=429, headers={"Retry-After": "0"})

    await attempt(rig.gateway.raise_stop("e-1", D("97")))

    assert rig.fake.uncovered_base() == 0
    assert rig.gateway.entry_state("e-1") == "closed"
    assert any(r.method == "POST" and r.path == "/api/v3/order" for r in rig.fake.log)


async def test_ban_on_the_replacement_oco_halts_with_the_position_exposed_and_alerted(rig: Rig) -> None:
    await protected(rig)
    rig.fake.fail("/api/v3/orderList/oco", times=1, status=418, msg="banned until 99999999999999")

    error = await attempt(rig.gateway.raise_stop("e-1", D("97")))

    assert isinstance(error, ProtectionError)
    assert rig.gateway.halted
    assert rig.gateway.entry_state("e-1") == "exposed"


async def test_unexpected_exception_type_in_the_window_still_escalates(rig: Rig) -> None:
    await protected(rig)

    async def boom(*_: Any, **__: Any) -> None:
        raise RuntimeError("bug inesperado")

    rig.gateway._post_oco = boom  # type: ignore[method-assign]

    await attempt(rig.gateway.raise_stop("e-1", D("97")))

    assert rig.fake.uncovered_base() == 0
    assert rig.gateway.entry_state("e-1") == "closed"


async def test_balance_read_failure_while_resizing_the_replacement_escalates_and_never_leaks(
    rig: Rig,
) -> None:
    await protected(rig)
    rig.fake.fail(
        "/api/v3/orderList/oco", times=1, kind="binance", status=400, code=-2010, msg="insufficient"
    )
    rig.fake.fail("/api/v3/account", times=99, status=503)

    await attempt(rig.gateway.raise_stop("e-1", D("97")))

    assert rig.gateway.halted
    assert rig.gateway.entry_state("e-1") == "exposed"
