from decimal import Decimal as D

import pytest

from engine.domain.gateway import DuplicateOrderError
from engine.domain.order import OrderStatus, OrderType
from engine.infrastructure.backtest import FillKind
from engine.infrastructure.paper import PaperGateway, PaperState
from tests.unit.paper.helpers import FakeMarket, book, candle, entry, make, opened


async def _trade(gateway: PaperGateway, clock, coid: str) -> None:  # type: ignore[no-untyped-def]
    gateway.on_book(book("99", "100"))
    await gateway.place_order(entry(coid, stop="90", target="120"))
    clock.advance(ms=100)
    gateway.on_book(book("99", "100"))
    clock.advance(ms=100)
    gateway.on_book(book("121", "122"))


async def test_compact_is_the_default_and_drops_terminal_orders_and_ocos() -> None:
    gateway, clock = make()
    await _trade(gateway, clock, "e1")
    state = gateway.snapshot()
    assert state.orders == ()
    assert state.ocos == ()
    assert len(gateway.snapshot(compact=False).orders) == 1


async def test_compact_snapshot_round_trips_through_json_and_restore() -> None:
    gateway, clock = await opened()
    gateway.on_candle(candle(5, "100", "101", "99", "100"))
    state = gateway.snapshot()
    reloaded = PaperState.model_validate(state.model_dump(mode="json"))
    restored = PaperGateway.restore(reloaded, FakeMarket(), clock)
    assert restored.snapshot() == state
    assert await restored.get_balances() == await gateway.get_balances()
    assert restored.positions == gateway.positions
    assert restored.open_protective_state() == gateway.open_protective_state()


async def test_compact_snapshot_size_does_not_grow_with_the_number_of_trades() -> None:
    gateway, clock = make(initial_quote=D(1_000_000))
    sizes = []
    for i in range(60):
        await _trade(gateway, clock, f"e{i}")
        sizes.append(len(gateway.snapshot().model_dump_json()))
    assert len(gateway.snapshot(compact=False).fills) == 120
    assert len(gateway.snapshot().fills) <= 20
    assert sizes[-1] - sizes[29] < (sizes[29] - sizes[0]) / 2 + 1  # satura: so crescem os ids dedupe
    assert sizes[-1] < len(gateway.snapshot(compact=False).model_dump_json()) / 2


async def test_fill_sequence_and_dedupe_survive_a_compact_restore() -> None:
    gateway, clock = make()
    await _trade(gateway, clock, "e1")
    restored = PaperGateway.restore(gateway.snapshot(), FakeMarket(), clock)
    with pytest.raises(DuplicateOrderError):
        await restored.place_order(entry("e1"))
    await _trade(restored, clock, "e2")
    assert [f.sequence for f in restored.fills] == [1, 2, 3, 4]


async def test_compact_restore_reproduces_the_same_stop_behaviour() -> None:
    full, clock = await opened(stop="90", target="120")
    compact = PaperGateway.restore(full.snapshot(), FakeMarket(), clock)
    complete = PaperGateway.restore(full.snapshot(compact=False), FakeMarket(), clock)
    for gateway in (compact, complete):
        gateway.on_candle(candle(3, "100", "100", "80", "85"))
    assert [f.kind for f in compact.fills] == [FillKind.ENTRY_MARKET, FillKind.STOP_LOSS]
    assert compact.fills[-1] == complete.fills[-1]
    assert compact.snapshot().balances == complete.snapshot().balances


async def test_open_orders_survive_a_compact_restore() -> None:
    gateway, clock = make()
    gateway.on_book(book("99", "100"))
    await gateway.place_order(entry("e1", price="95", kind=OrderType.LIMIT))
    restored = PaperGateway.restore(gateway.snapshot(), FakeMarket(), clock)
    assert (await restored.get_order("BTC/USDT", "e1")).status is OrderStatus.NEW
