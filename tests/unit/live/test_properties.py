import asyncio
import os
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from engine.application.risk.sizing import ceil_to_step, floor_to_step
from engine.domain.gateway import UnknownOrderError
from engine.domain.order import ExitOrder, OrderType
from engine.infrastructure.live.errors import LiveError
from engine.infrastructure.live.filters import PercentPrice, SymbolFilters
from tests.fakes.fake_binance import FakeBinance
from tests.unit.live.support import NOW, SYMBOL, Rig, approved_entry, build_rig

D = Decimal
steps = st.sampled_from([D("0.00001"), D("0.001"), D("0.01"), D("0.5"), D(1)])
amounts = st.decimals(min_value=D("0.0001"), max_value=D(100_000), places=6)


def make_filters(tick: Decimal, step: Decimal) -> SymbolFilters:
    return SymbolFilters(
        symbol=SYMBOL,
        trading=True,
        spot_allowed=True,
        margin_allowed=False,
        order_types=frozenset({"LIMIT"}),
        oco_allowed=True,
        opo_allowed=True,
        tick_size=tick,
        min_price=tick,
        max_price=D(1_000_000),
        step_size=step,
        min_qty=step,
        max_qty=D(1_000_000),
        min_notional=D(10),
        max_notional=None,
        percent_price=PercentPrice(bid_up=D(5), bid_down=D("0.2"), ask_up=D(5), ask_down=D("0.2")),
    )


@given(value=amounts, step=steps)
def test_rounding_down_and_up_are_aligned_and_bracket_the_value(value: Decimal, step: Decimal) -> None:
    down, up = floor_to_step(value, step), ceil_to_step(value, step)

    assert down <= value <= up
    assert down % step == 0
    assert up % step == 0
    assert up - down in (0, step)


@given(value=amounts, tick=steps, step=steps)
def test_filters_accept_aligned_values_and_reject_misaligned_ones(
    value: Decimal, tick: Decimal, step: Decimal
) -> None:
    filters = make_filters(tick, step)
    price = max(filters.round_price_down(value), tick)
    quantity = max(filters.round_quantity_down(value), step)
    ok = filters.problems(quantity=quantity, price=price, side="BUY", avg_price=price)
    assert [p for p in ok if not p.startswith("NOTIONAL")] == []
    if step > D("0.00001") and quantity > step:
        bad = filters.problems(quantity=quantity + step / 3, price=price, side="BUY", avg_price=price)
        assert any(p.startswith("LOT_SIZE") for p in bad)
    if tick > D("0.00001") and price > tick:
        bad = filters.problems(quantity=quantity, price=price + tick / 3, side="BUY", avg_price=price)
        assert any(p.startswith("PRICE_FILTER") for p in bad)


@given(value=amounts, step=steps)
def test_quantity_rounding_never_exceeds_requested(value: Decimal, step: Decimal) -> None:
    assert make_filters(step, step).round_quantity_down(value) <= value


FAULT_PATHS = [
    ("POST", "/api/v3/orderList/opoco"),
    ("POST", "/api/v3/orderList/oco"),
    ("POST", "/api/v3/order"),
    ("DELETE", "/api/v3/orderList"),
    ("DELETE", "/api/v3/order"),
    ("GET", "/api/v3/order"),
    ("GET", "/api/v3/orderList"),
    ("GET", "/api/v3/account"),
]
FAULT_KINDS = ["binance", "http", "after", "timeout", "would_trigger", "ban", "rate_limit"]
fault_spec = st.tuples(
    st.integers(0, len(FAULT_PATHS) - 1), st.integers(0, len(FAULT_KINDS) - 1), st.integers(1, 4)
)

op_strategy = st.one_of(
    st.tuples(st.just("price"), st.integers(min_value=90, max_value=115)),
    st.tuples(st.just("poll"), st.just(0)),
    st.tuples(st.just("place"), st.just(0)),
    st.tuples(st.just("raise"), st.integers(min_value=95, max_value=112)),
    st.tuples(st.just("drop"), st.just(0)),
    st.tuples(st.just("partial"), st.just(0)),
    st.tuples(st.just("noact"), st.booleans()),
    st.tuples(st.just("hold"), st.booleans()),
    st.tuples(st.just("exit"), st.integers(min_value=1, max_value=10)),
    st.tuples(st.just("fault"), fault_spec),
    st.tuples(st.just("fault2"), st.tuples(fault_spec, fault_spec)),
)


def inject(fake: FakeBinance, spec: tuple[int, int, int]) -> None:
    method, path = FAULT_PATHS[spec[0]]
    match FAULT_KINDS[spec[1]]:
        case "binance":
            fake.fail(path, method=method, times=spec[2], kind="binance", status=400, code=-1000)
        case "http":
            fake.fail(path, method=method, times=spec[2], status=503)
        case "after":
            fake.fail(path, method=method, times=spec[2], after=True)
        case "timeout":
            fake.fail(path, method=method, times=min(spec[2], 2), kind="timeout")
        case "ban":
            fake.fail(path, method=method, times=1, status=418, msg="banned until 99999999999999")
        case "rate_limit":
            fake.fail(path, method=method, times=spec[2], status=429, headers={"Retry-After": "0"})
        case _:
            fake.fail(path, method=method, times=1, kind="binance", status=400, code=-2021)


async def apply(rig: Rig, name: str, arg: Any, placed: int) -> None:
    gateway, fake = rig.gateway, rig.fake
    match name:
        case "price":
            fake.set_price("BTCUSDT", D(arg))
        case "poll":
            await gateway.poll()
        case "place" if placed < 3:
            await gateway.place_order(approved_entry(cid=f"e-{placed}"))
        case "raise":
            for entry_id in gateway.open_entry_ids():
                await gateway.raise_stop(entry_id, D(arg))
        case "drop":
            for entry_id in gateway.open_entry_ids():
                await gateway.drop_target(entry_id)
        case "partial":
            fake.fill_fraction = D("0.5")
        case "noact":
            fake.fail_activation = bool(arg)
        case "hold":
            fake.hold_activation = bool(arg)
        case "exit":
            order = ExitOrder(
                client_order_id=f"x-{len(fake.log)}",
                symbol=SYMBOL,
                type=OrderType.MARKET,
                quantity=D(arg),
                created_at=NOW,
            )
            await gateway.place_order(order)
        case "fault":
            inject(fake, arg)
        case "fault2":
            inject(fake, arg[0])
            inject(fake, arg[1])


async def run_sequence(ops: list[tuple[str, Any]]) -> tuple[FakeBinance, bool, list[tuple[str, str]]]:
    fake = FakeBinance()
    await fake.start()
    rig = build_rig(
        fake,
        request_timeout_seconds=0.12,
        partial_fill_grace_seconds=0,
        protection_activation_grace_seconds=0,
        read_retries=1,
    )
    placed = 0
    try:
        for name, arg in ops:
            try:
                await apply(rig, name, arg, placed)
                placed += name == "place"
            except (LiveError, ValueError, UnknownOrderError):
                pass
        fake.faults.clear()
        rig.client._banned_until = 0.0
        fake.fail_activation = False
        fake.hold_activation = False
        for listing in list(fake.lists.values()):
            if listing.kind == "OPOCO" and fake.orders[listing.order_ids[1]].status == "PENDING_NEW":
                fake.activate_pending(listing.cid)
        for _ in range(3):
            try:
                await rig.gateway.poll()
            except LiveError:
                break
        return fake, rig.gateway.halted, rig.alerts
    finally:
        await rig.client.aclose()
        await fake.stop()


@settings(
    max_examples=int(os.environ.get("LIVE_PROP_EXAMPLES", "200")),
    deadline=None,
    suppress_health_check=list(HealthCheck),
)
@given(ops=st.lists(op_strategy, min_size=3, max_size=14))
def test_position_is_never_left_exposed_under_random_failures(ops: list[tuple[str, Any]]) -> None:
    fake, halted, alerts = asyncio.run(run_sequence(ops))

    exposed = fake.uncovered_base()
    covered_or_sold = exposed < D("0.001")
    halted_with_alert = halted and any(level == "critical" and "PAUSADO" in msg for level, msg in alerts)
    assert covered_or_sold or halted_with_alert, f"posicao exposta ({exposed} BTC) sem pausa/alerta"


@pytest.mark.parametrize("fault_times", [1, 2])
def test_known_sequence_with_replace_faults_stays_covered(fault_times: int) -> None:
    ops: list[tuple[str, Any]] = [
        ("place", 0),
        ("price", 99),
        ("poll", 0),
        ("fault", (1, 0, fault_times)),
        ("price", 104),
        ("raise", 100),
    ]
    fake, halted, _ = asyncio.run(run_sequence(ops))

    assert fake.uncovered_base() < D("0.001")
    assert not halted
