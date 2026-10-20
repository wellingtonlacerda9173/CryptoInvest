from decimal import Decimal as D

import pytest

from engine.domain.gateway import DuplicateOrderError, ProtectedOrderGateway, UnknownOrderError
from engine.domain.order import OrderStatus, OrderType
from engine.domain.trade import ExitReason
from engine.infrastructure.backtest import BacktestGateway, FillKind, Liquidity
from engine.infrastructure.paper import MarketDataStalledError, PaperConfig, PaperGateway
from tests.unit.paper.helpers import (
    SYMBOL,
    FakeClock,
    FakeMarket,
    book,
    candle,
    entry,
    make,
    opened,
    sell,
)


def test_protected_order_gateway_protocol_is_shared() -> None:
    paper: ProtectedOrderGateway = PaperGateway(FakeMarket(), FakeClock())
    backtest: type[ProtectedOrderGateway] = BacktestGateway
    assert paper is not None
    assert backtest is BacktestGateway


class TestConfig:
    def test_defaults(self) -> None:
        config = PaperConfig()
        assert config.initial_quote == 10_000
        assert (config.maker_fee, config.taker_fee) == (D("0.001"), D("0.001"))

    def test_rejects_mixed_quote_assets(self) -> None:
        with pytest.raises(ValueError, match="quoted"):
            PaperConfig(symbols=("BTC/USDT", "ETH/BTC"))


class TestMarketOrders:
    async def test_without_book_is_rejected_and_never_invents_a_price(self) -> None:
        gateway, clock = make()
        report = await gateway.place_order(entry())
        clock.advance(seconds=5)
        assert report.status is OrderStatus.REJECTED
        assert gateway.rejection_reason("e1") == "no order book available"
        assert gateway.fills == ()
        balances = await gateway.get_balances()
        assert balances["USDT"].locked == 0

    async def test_stale_book_is_rejected(self) -> None:
        gateway, clock = make(max_book_age_seconds=10)
        gateway.on_book(book("99", "100"))
        clock.advance(seconds=11)
        report = await gateway.place_order(entry())
        assert report.status is OrderStatus.REJECTED

    async def test_fills_at_ask_with_slippage_and_taker_fee_only_after_latency(self) -> None:
        gateway, clock = make(slippage_bps=D(10), latency_ms=100)
        gateway.on_book(book("99", "100"))
        report = await gateway.place_order(entry())
        assert report.status is OrderStatus.NEW

        clock.advance(ms=99)
        gateway.on_book(book("99", "100"))
        assert gateway.fills == ()

        clock.advance(ms=1)
        gateway.on_book(book("99", "101"))
        (fill,) = gateway.fills
        assert fill.kind is FillKind.ENTRY_MARKET
        assert fill.liquidity is Liquidity.TAKER
        assert fill.price == D("101") * D("1.001")
        assert fill.slippage_cost == D("101") * D("0.001")
        assert fill.fee == fill.price * D("0.001")
        assert fill.at == clock.now()
        balances = await gateway.get_balances()
        assert balances["USDT"].free == D(10_000) - fill.price - fill.fee
        assert balances["BTC"].free == 0
        assert balances["BTC"].locked == 1
        assert (await gateway.get_order(SYMBOL, "e1")).status is OrderStatus.FILLED

    async def test_walks_the_book_and_rejects_when_depth_is_insufficient(self) -> None:
        gateway, clock = make()
        deep = book("99", "100", asks=[("100", "1"), ("102", "1")])
        gateway.on_book(deep)
        await gateway.place_order(entry("a", qty="2"))
        await gateway.place_order(entry("b", qty="3", stop="90"))
        clock.advance(ms=100)
        gateway.on_book(deep)
        assert gateway.fills[0].price == D(101)
        assert (await gateway.get_order(SYMBOL, "b")).status is OrderStatus.REJECTED
        assert gateway.rejection_reason("b") in {
            "insufficient liquidity",
            "insufficient quote balance at fill",
        }

    async def test_market_exit_sells_at_bid_minus_slippage_with_taker_fee(self) -> None:
        gateway, clock = await opened(slippage_bps=D(10), qty="2", stop="50", target="500")
        await gateway.place_order(sell(qty="1"))
        clock.advance(ms=100)
        gateway.on_book(book("100", "101"))
        fill = gateway.fills[-1]
        assert fill.kind is FillKind.EXIT_MARKET
        assert fill.price == D(100) * D("0.999")
        assert fill.exit_reason is ExitReason.MANUAL
        assert gateway.positions[SYMBOL].quantity == 1


class TestLimitEntry:
    async def test_not_touched_then_touched_fills_at_limit_with_maker_fee(self) -> None:
        gateway, clock = make()
        await gateway.place_order(entry(kind=OrderType.LIMIT, price="100"))
        clock.advance(ms=100)
        gateway.on_book(book("100", "100.5"))
        assert gateway.fills == ()
        assert (await gateway.get_balances())["USDT"].locked > 0

        gateway.on_book(book("98", "99"))
        (fill,) = gateway.fills
        assert (fill.kind, fill.liquidity, fill.price) == (FillKind.ENTRY_LIMIT, Liquidity.MAKER, D(100))
        assert fill.fee == D("0.1")
        assert fill.slippage_cost == 0
        assert (await gateway.get_balances())["USDT"].locked == 0

    async def test_limit_stays_open_until_cancelled(self) -> None:
        gateway, clock = make()
        await gateway.place_order(entry(kind=OrderType.LIMIT))
        clock.advance(seconds=3600)
        gateway.on_book(book("105", "106"))
        report = await gateway.cancel_order(SYMBOL, "e1")
        assert report.status is OrderStatus.CANCELED
        assert (await gateway.get_balances())["USDT"].locked == 0
        gateway.on_book(book("98", "99"))
        assert gateway.fills == ()

    async def test_limit_entry_expires_after_ttl(self) -> None:
        gateway, clock = make(entry_limit_ttl_seconds=60)
        await gateway.place_order(entry(kind=OrderType.LIMIT))
        clock.advance(seconds=61)
        gateway.on_book(book("98", "99"))
        assert (await gateway.get_order(SYMBOL, "e1")).status is OrderStatus.EXPIRED
        assert gateway.fills == ()

    async def test_cancel_after_fill_keeps_filled(self) -> None:
        gateway, _ = await opened()
        assert (await gateway.cancel_order(SYMBOL, "e1")).status is OrderStatus.FILLED


class TestOcoProtection:
    async def test_stop_hits_on_tick_with_slippage_taker_fee_and_stop_reason(self) -> None:
        gateway, clock = await opened(slippage_bps=D(10))
        clock.advance(ms=10)
        gateway.on_book(book("90", "91"))
        stop = gateway.fills[-1]
        assert stop.kind is FillKind.STOP_LOSS
        assert stop.liquidity is Liquidity.TAKER
        assert stop.exit_reason is ExitReason.STOP_LOSS
        assert stop.price == D(90) * D("0.999")
        assert stop.slippage_cost == D(90) * D("0.001")
        assert (await gateway.get_order(SYMBOL, "e1:oco")).status is OrderStatus.FILLED
        assert gateway.positions == {}

    async def test_stop_is_active_in_the_very_tick_that_fills_the_entry(self) -> None:
        gateway, clock = make()
        await gateway.place_order(entry(kind=OrderType.LIMIT, price="100"))
        clock.advance(ms=100)
        gateway.on_book(book("85", "99"))
        assert [f.kind for f in gateway.fills] == [FillKind.ENTRY_LIMIT, FillKind.STOP_LOSS]
        assert gateway.fills[-1].price == D(85)

    async def test_gap_below_stop_fills_at_current_bid_not_at_stop(self) -> None:
        gateway, clock = await opened()
        clock.advance(seconds=60)
        gateway.on_book(book("80", "81", bids=[("80", "0.5"), ("70", "10")]))
        assert gateway.fills[-1].price == D(75)

    async def test_target_fills_as_maker_at_target(self) -> None:
        gateway, clock = await opened(slippage_bps=D(10))
        clock.advance(ms=10)
        gateway.on_book(book("125", "126"))
        fill = gateway.fills[-1]
        assert (fill.kind, fill.liquidity, fill.price) == (FillKind.TAKE_PROFIT, Liquidity.MAKER, D(120))
        assert fill.exit_reason is ExitReason.TAKE_PROFIT
        assert fill.slippage_cost == 0

    async def test_target_is_not_evaluated_in_the_entry_tick(self) -> None:
        gateway, clock = make()
        gateway.on_book(book("99", "100"))
        await gateway.place_order(entry(target="120"))
        clock.advance(ms=100)
        gateway.on_book(book("125", "126"))
        assert [f.kind for f in gateway.fills] == [FillKind.ENTRY_MARKET]

    async def test_stop_and_target_in_the_same_interval_resolve_to_stop(self) -> None:
        gateway, clock = await opened()
        clock.advance(seconds=60)
        gateway.on_candle(candle(1, "100", "130", "85", "110"))
        assert gateway.fills[-1].kind is FillKind.STOP_LOSS
        assert gateway.fills[-1].price == D(90)
        assert len(gateway.fills) == 2

    async def test_candle_covers_stop_touched_between_books(self) -> None:
        gateway, clock = await opened()
        clock.advance(seconds=120)
        gateway.on_candle(candle(1, "100", "101", "88", "99"))
        assert gateway.fills[-1].kind is FillKind.STOP_LOSS

    async def test_candle_gap_below_stop_fills_at_open(self) -> None:
        gateway, clock = await opened()
        clock.advance(seconds=120)
        gateway.on_candle(candle(1, "80", "95", "78", "90"))
        assert gateway.fills[-1].price == D(80)

    async def test_candle_that_started_before_the_entry_is_ignored(self) -> None:
        gateway, _ = await opened()
        gateway.on_candle(candle(0, "100", "130", "80", "100"))
        assert len(gateway.fills) == 1

    async def test_candles_are_deduplicated_and_unclosed_ignored(self) -> None:
        gateway, clock = await opened()
        clock.advance(seconds=120)
        gateway.on_candle(candle(1, "100", "101", "99", "100"))
        first_update = gateway.last_update_at
        clock.advance(seconds=1)
        gateway.on_candle(candle(1, "100", "101", "1", "100"))
        assert gateway.last_update_at == first_update
        assert len(gateway.fills) == 1


class TestStopManagement:
    async def test_raise_stop_only_goes_up(self) -> None:
        gateway, clock = await opened()
        clock.advance(ms=10)
        gateway.on_book(book("110", "111"))
        assert gateway.raise_stop("e1", D(100)).status is OrderStatus.NEW
        with pytest.raises(ValueError, match="raised"):
            gateway.raise_stop("e1", D(95))
        with pytest.raises(ValueError, match="raised"):
            gateway.raise_stop("e1", D(100))

    async def test_raise_stop_must_stay_below_bid_and_target(self) -> None:
        gateway, clock = await opened()
        clock.advance(ms=10)
        gateway.on_book(book("110", "111"))
        with pytest.raises(ValueError, match="below"):
            gateway.raise_stop("e1", D(110))
        with pytest.raises(ValueError, match="below"):
            gateway.raise_stop("e1", D(120))

    async def test_raised_stop_is_the_one_that_triggers(self) -> None:
        gateway, clock = await opened()
        clock.advance(ms=10)
        gateway.on_book(book("110", "111"))
        gateway.raise_stop("e1", D(105))
        gateway.on_book(book("104", "105"))
        assert gateway.fills[-1].kind is FillKind.STOP_LOSS
        assert gateway.fills[-1].price == D(104)

    async def test_raise_stop_without_fresh_price_or_unknown_order(self) -> None:
        gateway, clock = await opened()
        clock.advance(seconds=60)
        with pytest.raises(ValueError, match="no current price"):
            gateway.raise_stop("e1", D(95))
        with pytest.raises(UnknownOrderError):
            gateway.raise_stop("nope", D(95))
        with pytest.raises(UnknownOrderError):
            gateway.drop_target("nope")

    async def test_drop_target_turns_oco_into_pure_stop(self) -> None:
        gateway, clock = await opened()
        gateway.drop_target("e1")
        clock.advance(ms=10)
        gateway.on_book(book("130", "131"))
        assert len(gateway.fills) == 1
        gateway.on_book(book("89", "90"))
        assert gateway.fills[-1].kind is FillKind.STOP_LOSS

    async def test_partial_by_limit_sell_shrinks_the_oco(self) -> None:
        gateway, clock = await opened(qty="2", target="500")
        await gateway.place_order(sell(kind=OrderType.LIMIT, qty="1", price="110"))
        clock.advance(ms=100)
        gateway.on_book(book("111", "112"))
        partial = gateway.fills[-1]
        assert (partial.kind, partial.liquidity, partial.price) == (
            FillKind.EXIT_LIMIT,
            Liquidity.MAKER,
            D(110),
        )
        assert gateway.positions[SYMBOL].quantity == 1
        balances = await gateway.get_balances()
        assert balances["BTC"].locked == 1
        gateway.on_book(book("89", "90"))
        assert gateway.fills[-1].quantity == 1
        assert gateway.fills[-1].kind is FillKind.STOP_LOSS

    async def test_limit_sell_is_not_filled_in_a_tick_that_also_hits_the_stop(self) -> None:
        gateway, clock = await opened(qty="2", target="500")
        await gateway.place_order(sell(kind=OrderType.LIMIT, qty="1", price="95"))
        clock.advance(ms=100)
        gateway.on_candle(candle(2, "100", "112", "88", "100"))
        assert gateway.fills[-1].kind is FillKind.STOP_LOSS
        assert gateway.fills[-1].quantity == 2
        assert (await gateway.get_order(SYMBOL, "x1")).status is OrderStatus.NEW


class TestBalanceAndIds:
    async def test_insufficient_quote_is_rejected_report_not_exception(self) -> None:
        gateway, _ = make(initial_quote=D(150))
        gateway.on_book(book("99", "100"))
        first = await gateway.place_order(entry("a", kind=OrderType.LIMIT, price="100"))
        second = await gateway.place_order(entry("b", kind=OrderType.LIMIT, price="100"))
        assert first.status is OrderStatus.NEW
        assert second.status is OrderStatus.REJECTED
        assert gateway.rejection_reason("b") == "insufficient quote balance"

    async def test_exit_without_position_is_rejected(self) -> None:
        gateway, _ = make()
        report = await gateway.place_order(sell(kind=OrderType.LIMIT, price="110"))
        assert report.status is OrderStatus.REJECTED

    async def test_unknown_symbol_is_rejected(self) -> None:
        gateway, _ = make(symbols=("ETH/USDT",))
        report = await gateway.place_order(entry(kind=OrderType.LIMIT))
        assert report.status is OrderStatus.REJECTED
        assert gateway.rejection_reason("e1") == "unknown symbol"

    async def test_duplicate_client_order_id_raises_and_ids_are_stable(self) -> None:
        gateway, _ = make()
        first = await gateway.place_order(entry(kind=OrderType.LIMIT))
        with pytest.raises(DuplicateOrderError):
            await gateway.place_order(entry(kind=OrderType.LIMIT))
        again = await gateway.get_order(SYMBOL, "e1")
        assert (first.order_id, again.order_id) == ("paper-1", "paper-1")

    async def test_oco_suffix_is_reserved(self) -> None:
        gateway, _ = make()
        with pytest.raises(ValueError, match="must not end"):
            await gateway.place_order(entry("x:oco", kind=OrderType.LIMIT))

    async def test_unknown_orders(self) -> None:
        gateway, _ = make()
        with pytest.raises(UnknownOrderError):
            await gateway.get_order(SYMBOL, "nope")
        with pytest.raises(UnknownOrderError):
            await gateway.cancel_order(SYMBOL, "nope:oco")

    async def test_fills_are_append_only_and_sequenced(self) -> None:
        gateway, clock = await opened()
        before = gateway.fills
        clock.advance(ms=10)
        gateway.on_book(book("85", "86"))
        assert gateway.fills[: len(before)] == before
        assert [f.sequence for f in gateway.fills] == [1, 2]

    async def test_position_tracking_and_realized_pnl(self) -> None:
        gateway, clock = await opened()
        assert gateway.positions[SYMBOL].average_price == 100
        clock.advance(ms=10)
        gateway.on_book(book("125", "126"))
        assert gateway.positions == {}
        position = gateway.snapshot().positions[0]
        assert (position.quantity, position.realized_pnl) == (0, 20)
        assert position.fees_paid == D("0.1") + D("0.12")


class TestStreams:
    async def test_run_consumes_the_book_stream_and_drives_fills(self) -> None:
        gateway, clock = make(FakeMarket(books=[book("99", "100")]))
        gateway.on_book(book("99", "100"))
        await gateway.place_order(entry())
        clock.advance(ms=100)
        with pytest.raises(ExceptionGroup) as raised:
            await gateway.run()
        assert raised.group_contains(MarketDataStalledError)
        assert [f.kind for f in gateway.fills] == [FillKind.ENTRY_MARKET]

    async def test_stream_candles_feeds_the_simulation_before_yielding(self) -> None:
        market = FakeMarket(candles=[candle(1, "100", "101", "80", "95")])
        gateway, clock = make(market)
        gateway.on_book(book("99", "100"))
        await gateway.place_order(entry())
        clock.advance(ms=100)
        gateway.on_book(book("99", "100"))
        clock.advance(seconds=120)
        seen = [c async for c in gateway.stream_candles(SYMBOL, market.candles[0].timeframe)]
        assert len(seen) == 1
        assert gateway.fills[-1].kind is FillKind.STOP_LOSS

    async def test_fetch_candles_and_order_book_delegate_to_the_source(self) -> None:
        market = FakeMarket(books=[book("99", "100")], candles=[candle(1, "1", "2", "1", "2")])
        gateway, _ = make(market)
        assert (
            len(await gateway.fetch_candles(SYMBOL, market.candles[0].timeframe, market.candles[0].open_time))
            == 1
        )
        assert len([b async for b in gateway.stream_order_book(SYMBOL)]) == 1

    async def test_book_of_other_symbols_or_one_sided_is_ignored(self) -> None:
        gateway, _ = make()
        gateway.on_book(book("1", "2", symbol="ETH/USDT"))
        assert gateway.last_update_at is None
        assert gateway.best_prices(SYMBOL) is None
