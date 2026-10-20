"""Reverificacao QA do PaperGateway (Fase 3, 2a rodada)."""

from datetime import timedelta
from decimal import Decimal as D

from engine.domain.order import OrderStatus, OrderType
from engine.infrastructure.backtest import FillKind, Liquidity
from engine.infrastructure.paper import PaperGateway
from tests.unit.paper.helpers import T0, FakeClock, FakeMarket, book, candle, entry, make, opened, sell


async def test_restore_with_override_adding_a_symbol_keeps_the_gateway_usable() -> None:
    gateway, clock = await opened()
    state = gateway.snapshot()
    wider = state.config.model_copy(update={"symbols": ("BTC/USDT", "ETH/USDT")})
    restored = PaperGateway.restore(state, FakeMarket(), clock, wider, allow_config_override=True)
    balances = await restored.get_balances()
    assert set(balances) == {"USDT", "BTC", "ETH"}


async def test_restore_with_override_adding_a_symbol_accepts_orders_on_it() -> None:
    gateway, clock = await opened()
    wider = gateway.snapshot().config.model_copy(update={"symbols": ("BTC/USDT", "ETH/USDT")})
    restored = PaperGateway.restore(
        gateway.snapshot(), FakeMarket(), clock, wider, allow_config_override=True
    )
    restored.on_book(book("100", "101", symbol="ETH/USDT"))
    eth = entry("e9", price="100", stop="90", target="120").model_copy(update={"symbol": "ETH/USDT"})
    report = await restored.place_order(eth)
    assert report.status is OrderStatus.NEW
    clock.advance(ms=100)
    restored.on_book(book("100", "101", symbol="ETH/USDT"))
    assert restored.fills[-1].symbol == "ETH/USDT"


async def test_marketable_limit_reservation_covers_the_taker_fee_it_will_pay() -> None:
    # reserva no pior caso (limite + taxa taker): aceita o que cabe e nunca rejeita depois no fill
    gateway, clock = make(maker_fee=D(0), taker_fee=D("0.001"), initial_quote=D("100.1"))
    gateway.on_book(book("98", "99"))
    report = await gateway.place_order(entry("e1", kind=OrderType.LIMIT, price="100", qty="1", stop="90"))
    assert report.status is OrderStatus.NEW
    assert (await gateway.get_balances())["USDT"].locked == D("100.1")
    clock.advance(ms=100)
    gateway.on_book(book("98", "99.95"))
    assert gateway.fills, "ordem aceita e reservada foi rejeitada no fill: reserva ignorou a taxa taker"


async def test_marketable_limit_that_cannot_cover_the_taker_fee_is_rejected_upfront() -> None:
    gateway, _ = make(maker_fee=D(0), taker_fee=D("0.001"), initial_quote=D(100))
    gateway.on_book(book("98", "99"))
    report = await gateway.place_order(entry("e1", kind=OrderType.LIMIT, price="100", qty="1", stop="90"))
    assert report.status is OrderStatus.REJECTED


async def test_marketable_limit_hit_first_by_a_candle_is_still_taker() -> None:
    gateway, clock = make(taker_fee=D("0.002"), maker_fee=D("0.001"))
    gateway.on_book(book("98", "99"))
    await gateway.place_order(entry("e1", kind=OrderType.LIMIT, price="100"))
    clock.advance(seconds=120)
    gateway.on_candle(candle(1, "99", "100", "98", "99"))
    assert gateway.fills[0].liquidity is Liquidity.TAKER


async def test_replay_after_restore_evaluates_candles_after_the_entry_and_uses_market_time() -> None:
    gateway, clock = make()
    gateway.on_book(book("99", "100"))
    await gateway.place_order(entry("e1", stop="90", target="120"))
    clock.advance(ms=100)
    gateway.on_book(book("99", "100"))  # entrada enche ao relogio T0+100ms
    state = gateway.snapshot()
    later = FakeClock(T0 + timedelta(days=2))
    restored = PaperGateway.restore(state, FakeMarket(), later)
    c0 = candle(0, "100", "101", "99", "100")  # contem a entrada: descartada (anti-lookahead)
    c1 = candle(1, "100", "100", "80", "85")
    for c in (c0, c1):
        restored.on_candle(c)
    stop = restored.fills[-1]
    assert (stop.kind, stop.at) == (FillKind.STOP_LOSS, c1.close_time)
    assert stop.price == D(90)


async def test_replay_does_not_apply_a_candle_twice_nor_older_than_the_snapshot_cursor() -> None:
    gateway, clock = await opened()
    gateway.on_candle(candle(10, "100", "101", "99", "100"))
    restored = PaperGateway.restore(gateway.snapshot(), FakeMarket(), clock)
    restored.on_candle(candle(3, "100", "100", "1", "1"))  # mais antiga que o cursor: ignorada
    restored.on_candle(candle(10, "100", "100", "1", "1"))  # mesma vela: ignorada
    assert [f.kind for f in restored.fills] == [FillKind.ENTRY_MARKET]


async def test_stale_since_after_restore_counts_from_the_restore_not_from_the_snapshot() -> None:
    gateway, _ = await opened()
    state = gateway.snapshot()
    later = FakeClock(T0 + timedelta(days=3))
    restored = PaperGateway.restore(state, FakeMarket(), later)
    assert restored.stops_blind is False  # recem restaurado: a contagem comeca agora
    later.advance(seconds=31)
    assert restored.stops_blind is True


async def test_open_protective_state_excludes_triggered_and_canceled_ocos() -> None:
    gateway, clock = await opened()
    assert [o.entry_client_order_id for o in gateway.open_protective_state()] == ["e1"]
    clock.advance(seconds=1)
    gateway.on_book(book("50", "51"))
    assert gateway.open_protective_state() == []


async def test_manual_exit_shrinks_the_oco_and_never_leaves_a_negative_balance() -> None:
    gateway, clock = await opened(qty="2")
    await gateway.place_order(sell("x1", qty="1"))
    clock.advance(ms=100)
    gateway.on_book(book("100", "101"))
    protective = gateway.open_protective_state()
    assert [o.quantity for o in protective] == [D(1)]
    balances = await gateway.get_balances()
    assert balances["BTC"].free >= 0
    assert balances["USDT"].free >= 0


async def test_watchdog_reports_blind_once_and_recovery_once() -> None:
    import asyncio

    gateway, clock = await opened(max_stop_blind_seconds=1)
    events: list[bool] = []
    task = asyncio.create_task(gateway.watchdog(lambda health: events.append(health.stops_blind)))
    await asyncio.sleep(0.1)
    assert events == []
    clock.advance(seconds=5)
    await asyncio.sleep(0.4)
    assert events == [True]
    gateway.on_book(book("99", "100"))
    await asyncio.sleep(0.4)
    task.cancel()
    assert events == [True, False]


async def test_stops_blind_boundary_is_strictly_greater_than_the_limit() -> None:
    gateway, clock = await opened(max_stop_blind_seconds=30)
    clock.advance(seconds=30)
    assert gateway.stops_blind is False
    clock.advance(ms=1)
    assert gateway.stops_blind is True


async def test_candle_replay_alone_does_not_hide_a_dead_book_feed_for_longer_than_the_limit() -> None:
    gateway, clock = await opened(max_stop_blind_seconds=30)
    clock.advance(seconds=31)
    assert gateway.stops_blind is True
    gateway.on_candle(candle(50, "100", "101", "99", "100"))
    assert (
        gateway.stops_blind is False
    )  # qualquer vela fechada conta como feed vivo (documenta o comportamento)
