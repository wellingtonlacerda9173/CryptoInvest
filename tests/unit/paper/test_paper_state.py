import ast
import json
from decimal import Decimal as D
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

import engine.infrastructure.paper as paper_package
from engine.domain.order import OrderStatus, OrderType
from engine.infrastructure.backtest import FillKind
from engine.infrastructure.paper import PaperGateway, PaperState
from tests.unit.paper.helpers import SYMBOL, FakeClock, FakeMarket, book, entry, make, opened, sell


def through_json(state: PaperState) -> PaperState:
    return PaperState.model_validate(json.loads(json.dumps(state.model_dump(mode="json"))))


async def busy_gateway() -> tuple[PaperGateway, FakeClock]:
    gateway, clock = await opened(qty="2", target="500", slippage_bps=D(5))
    await gateway.place_order(sell(kind=OrderType.LIMIT, qty="1", price="110"))
    await gateway.place_order(entry("e2", kind=OrderType.LIMIT, price="95", stop="90", target="120"))
    clock.advance(ms=10)
    gateway.on_book(book("101", "102"))
    return gateway, clock


async def test_snapshot_is_json_serializable_and_round_trips() -> None:
    gateway, _ = await busy_gateway()
    state = gateway.snapshot(compact=False)
    assert through_json(state) == state
    assert through_json(gateway.snapshot()) == gateway.snapshot()
    assert state.version == 1
    assert [o.order.client_order_id for o in state.orders] == ["e1", "x1", "e2"]
    assert [o.order.client_order_id for o in gateway.snapshot().orders] == ["x1", "e2"]
    assert [o.entry_client_order_id for o in state.ocos] == ["e1"]
    assert state.positions[0].quantity == 2
    assert state.order_seq == 4
    assert state.last_update_at is not None


async def test_restore_reproduces_the_exact_state() -> None:
    gateway, clock = await busy_gateway()
    state = through_json(gateway.snapshot())
    restored = PaperGateway.restore(state, FakeMarket(), clock)
    assert restored.snapshot() == state
    assert await restored.get_balances() == await gateway.get_balances()
    assert (await restored.get_order(SYMBOL, "e1:oco")) == (await gateway.get_order(SYMBOL, "e1:oco"))


async def test_restored_pending_orders_keep_working_and_ids_continue() -> None:
    gateway, clock = await busy_gateway()
    restored = PaperGateway.restore(through_json(gateway.snapshot()), FakeMarket(), clock)
    clock.advance(ms=100)
    restored.on_book(book("111", "112"))
    assert restored.fills[-1].kind is FillKind.EXIT_LIMIT
    report = await restored.place_order(
        entry("e3", kind=OrderType.LIMIT, price="80", stop="70", target="100")
    )
    assert report.order_id == "paper-5"


async def test_restore_keeps_stop_active_and_executes_at_worst_price_after_downtime() -> None:
    gateway, clock = await opened(slippage_bps=D(10))
    state = through_json(gateway.snapshot())

    clock.advance(seconds=3600)
    restored = PaperGateway.restore(state, FakeMarket(), clock)
    assert restored.best_prices(SYMBOL) is None
    assert (await restored.get_order(SYMBOL, "e1:oco")).status is OrderStatus.NEW

    restored.on_book(book("70", "71"))
    stop = restored.fills[-1]
    assert stop.kind is FillKind.STOP_LOSS
    assert stop.price == D(70) * D("0.999")
    assert restored.positions == {}


async def test_restore_without_breach_leaves_oco_working() -> None:
    gateway, clock = await opened()
    restored = PaperGateway.restore(through_json(gateway.snapshot()), FakeMarket(), clock)
    clock.advance(seconds=10)
    restored.on_book(book("100", "101"))
    assert len(restored.fills) == 1
    restored.on_book(book("90", "91"))
    assert restored.fills[-1].kind is FillKind.STOP_LOSS


async def test_restore_accepts_config_override() -> None:
    gateway, clock = await opened()
    state = gateway.snapshot()
    other = state.config.model_copy(update={"taker_fee": D("0.0005")})
    restored = PaperGateway.restore(state, FakeMarket(), clock, other, allow_config_override=True)
    assert restored.config.taker_fee == D("0.0005")


async def scripted() -> PaperState:
    gateway, clock = make(slippage_bps=D(7), latency_ms=50)
    steps = [("99", "100"), ("98", "99"), ("101", "102"), ("96", "97"), ("89", "90")]
    gateway.on_book(book(*steps[0]))
    await gateway.place_order(entry(kind=OrderType.LIMIT, price="99"))
    await gateway.place_order(entry("m", qty="2", stop="80", target="200"))
    for bid, ask in steps[1:]:
        clock.advance(ms=60)
        gateway.on_book(book(bid, ask))
    return gateway.snapshot()


async def test_same_inputs_give_the_same_state() -> None:
    first, second = await scripted(), await scripted()
    assert first == second
    assert first.model_dump_json() == second.model_dump_json()
    assert first.fills


def test_paper_package_has_no_network_or_exchange_imports() -> None:
    forbidden = {"ccxt", "aiohttp", "requests", "httpx", "websockets", "socket", "urllib"}
    for path in Path(paper_package.__file__ or "").parent.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            else:
                continue
            assert not {m.split(".")[0] for m in modules} & forbidden, path.name
            assert not any("binance" in m for m in modules), path.name


def test_gateway_exposes_no_real_order_methods() -> None:
    names = {name.lower() for name in dir(PaperGateway)}
    assert not {"create_order", "createorder", "send_order", "submit_live"} & names


prices = st.decimals(min_value=D(60), max_value=D(140), places=2)
spreads = st.decimals(min_value=D("0.01"), max_value=D(2), places=2)


@given(st.lists(st.tuples(prices, spreads), max_size=40))
@settings(max_examples=60, deadline=None)
async def test_position_is_never_left_without_an_active_stop(ticks: list[tuple[D, D]]) -> None:
    gateway, clock = make(slippage_bps=D(5), latency_ms=0)
    gateway.on_book(book("99", "100"))
    await gateway.place_order(entry(qty="1", stop="90", target="130"))
    for bid, spread in ticks:
        clock.advance(ms=10)
        gateway.on_book(book(str(bid), str(bid + spread)))
        balances = await gateway.get_balances()
        total = balances["BTC"].free + balances["BTC"].locked
        assert total == 0 or balances["BTC"].locked == total
        assert balances["USDT"].free >= 0
    assert all(f.price <= D(90) for f in gateway.fills if f.kind is FillKind.STOP_LOSS)
