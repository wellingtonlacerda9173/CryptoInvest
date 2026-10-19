from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from typing import Any

import pytest

from engine.domain.candle import Candle
from engine.domain.gateway import ExchangeGateway
from engine.domain.order import EntryOrder, ExitOrder, Order, OrderStatus, OrderType
from engine.domain.trade import ExitReason
from engine.domain.types import Timeframe
from engine.infrastructure.backtest import (
    BacktestConfig,
    BacktestGateway,
    DuplicateOrderError,
    FillKind,
    Liquidity,
    UnknownOrderError,
)
from engine.infrastructure.backtest import gateway as gateway_module
from tests.contract.exchange_gateway import ExchangeGatewayContract

T0 = datetime(2026, 1, 1, tzinfo=UTC)
SYMBOL = "BTC/USDT"
TF = Timeframe.H1


def candle(i: int, o: str, h: str, low: str, c: str) -> Candle:
    return Candle(
        symbol=SYMBOL,
        timeframe=TF,
        open_time=T0 + timedelta(hours=i),
        open=D(o),
        high=D(h),
        low=D(low),
        close=D(c),
        volume=D(1),
        closed=True,
    )


def flat(n: int, price: str = "100") -> list[Candle]:
    return [candle(i, price, price, price, price) for i in range(n)]


def entry(
    coid: str = "e1",
    *,
    kind: OrderType = OrderType.MARKET,
    qty: str = "1",
    price: str = "100",
    stop: str = "90",
    target: str = "120",
) -> EntryOrder:
    return EntryOrder(
        client_order_id=coid,
        symbol=SYMBOL,
        type=kind,
        quantity=D(qty),
        price=D(price),
        stop_loss=D(stop),
        take_profit=D(target),
        created_at=T0,
    )


def sell(
    coid: str = "x1", *, kind: OrderType = OrderType.MARKET, qty: str = "1", price: str | None = None
) -> ExitOrder:
    return ExitOrder(
        client_order_id=coid,
        symbol=SYMBOL,
        type=kind,
        quantity=D(qty),
        price=None if price is None else D(price),
        created_at=T0,
    )


def make(candles: list[Candle], **config: Any) -> BacktestGateway:
    return BacktestGateway(candles, BacktestConfig(**config))


async def balances(gw: BacktestGateway) -> tuple[D, D]:
    b = await gw.get_balances()
    return b["USDT"].free + b["USDT"].locked, b["BTC"].free + b["BTC"].locked


class TestBacktestGatewayContract(ExchangeGatewayContract):
    revalidate_module = gateway_module

    @pytest.fixture
    def gateway(self) -> Any:
        return make(flat(5))

    def sent_orders(self, gateway: ExchangeGateway) -> list[Order]:
        assert isinstance(gateway, BacktestGateway)
        return gateway.submitted_orders


class TestConstruction:
    def test_rejects_empty(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            BacktestGateway([])

    def test_rejects_gap_and_disorder(self) -> None:
        candles = flat(3)
        with pytest.raises(ValueError, match="contiguous"):
            BacktestGateway([candles[0], candles[2]])
        with pytest.raises(ValueError, match="contiguous"):
            BacktestGateway([candles[1], candles[0]])

    def test_rejects_open_candle(self) -> None:
        open_candle = flat(1)[0].model_copy(update={"closed": False})
        with pytest.raises(ValueError, match="closed"):
            BacktestGateway([open_candle])

    def test_latency_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="latency"):
            BacktestConfig(latency_candles=0)


class TestClockAndLookahead:
    async def test_only_revealed_candles_are_visible(self) -> None:
        gw = make(flat(4))
        assert gw.now is None
        assert await gw.fetch_candles(SYMBOL, TF, T0) == []
        gw.advance()
        gw.advance()
        visible = await gw.fetch_candles(SYMBOL, TF, T0)
        assert [c.open_time for c in visible] == [T0, T0 + timedelta(hours=1)]
        assert gw.now == T0 + timedelta(hours=2)

    async def test_fetch_respects_start_and_end(self) -> None:
        gw = make(flat(5))
        for _ in range(5):
            gw.advance()
        got = await gw.fetch_candles(SYMBOL, TF, T0 + timedelta(hours=1), T0 + timedelta(hours=3))
        assert [c.open_time.hour for c in got] == [1, 2, 3]

    async def test_advance_stops_at_end(self) -> None:
        gw = make(flat(1))
        assert gw.advance() is not None
        assert gw.advance() is None
        assert not gw.has_next

    async def test_stream_replays_history_then_drives_clock(self) -> None:
        gw = make(flat(5))
        gw.advance()
        gw.advance()
        seen = [c.open_time.hour async for c in gw.stream_candles(SYMBOL, TF)]
        assert seen == [0, 1, 2, 3, 4]
        assert gw.now == T0 + timedelta(hours=5)

    async def test_stream_since_filters_replay(self) -> None:
        gw = make(flat(4))
        gw.advance()
        gw.advance()
        seen = [c.open_time.hour async for c in gw.stream_candles(SYMBOL, TF, since=T0 + timedelta(hours=1))]
        assert seen == [1, 2, 3]

    async def test_stream_exposes_no_future_candle_to_consumer(self) -> None:
        gw = make(flat(4))
        async for c in gw.stream_candles(SYMBOL, TF):
            visible = await gw.fetch_candles(SYMBOL, TF, T0)
            assert visible[-1] == c

    async def test_wrong_market_rejected(self) -> None:
        gw = make(flat(2))
        with pytest.raises(ValueError, match="holds"):
            await gw.fetch_candles("ETH/USDT", TF, T0)
        with pytest.raises(ValueError, match="holds"):
            await gw.fetch_candles(SYMBOL, Timeframe.M5, T0)

    def test_order_book_unsupported(self) -> None:
        with pytest.raises(NotImplementedError):
            make(flat(2)).stream_order_book(SYMBOL)


class TestMarketFills:
    async def test_market_entry_fills_at_next_open_with_slippage_and_taker_fee(self) -> None:
        candles = [candle(0, "100", "100", "100", "100"), candle(1, "102", "110", "100", "105")]
        gw = make(candles, slippage_bps=D(10))
        gw.advance()
        report = await gw.place_order(entry(price="100", stop="90", target="130"))
        assert report.status is OrderStatus.NEW
        gw.advance()

        [fill] = gw.fills
        assert fill.kind is FillKind.ENTRY_MARKET
        assert fill.liquidity is Liquidity.TAKER
        assert fill.price == D("102") * D("1.001")
        assert fill.fee == fill.price * D("0.001")
        assert fill.slippage_cost == D("0.102")
        assert fill.at == T0 + timedelta(hours=1)
        usdt, btc = await balances(gw)
        assert usdt == D(10_000) - fill.price - fill.fee
        assert btc == 1

    def test_default_fees_are_binance(self) -> None:
        config = BacktestConfig()
        assert (config.maker_fee, config.taker_fee, config.slippage_bps) == (D("0.001"), D("0.001"), 0)

    async def test_order_placed_before_start_fills_on_first_candle(self) -> None:
        gw = make(flat(3))
        await gw.place_order(entry())
        gw.advance()
        assert len(gw.fills) == 1

    async def test_market_exit_slippage_against_trader(self) -> None:
        candles = [*flat(2), candle(2, "110", "110", "110", "110"), candle(3, "110", "110", "110", "110")]
        gw = make(candles, slippage_bps=D(100), taker_fee=D("0.002"))
        await gw.place_order(entry(stop="50", target="200"))
        gw.advance()
        gw.advance()
        await gw.place_order(sell())
        gw.advance()

        sell_fill = gw.fills[-1]
        assert sell_fill.kind is FillKind.EXIT_MARKET
        assert sell_fill.exit_reason is ExitReason.MANUAL
        assert sell_fill.price == D("110") * D("0.99")
        assert sell_fill.fee == sell_fill.price * D("0.002")
        assert sell_fill.slippage_cost == D("1.1")
        assert (await balances(gw))[1] == 0
        assert (await gw.get_order(SYMBOL, "e1:oco")).status is OrderStatus.CANCELED


class TestLatency:
    @pytest.mark.parametrize(("latency", "expected_index"), [(1, 1), (2, 2), (3, 3)])
    async def test_fills_only_after_latency(self, latency: int, expected_index: int) -> None:
        gw = make(flat(6), latency_candles=latency)
        gw.advance()
        await gw.place_order(entry(stop="50", target="200"))
        filled_at = None
        while (c := gw.advance()) is not None:
            if gw.fills:
                filled_at = int((c.open_time - T0).total_seconds() // 3600)
                break
        assert filled_at == expected_index

    async def test_no_fill_on_the_candle_of_submission(self) -> None:
        gw = make(flat(3))
        gw.advance()
        await gw.place_order(entry())
        assert gw.fills == ()


class TestLimitOrders:
    async def test_limit_buy_not_reached_stays_open(self) -> None:
        candles = [*flat(1), candle(1, "105", "108", "101", "105"), candle(2, "105", "106", "100", "103")]
        gw = make(candles)
        gw.advance()
        await gw.place_order(entry(kind=OrderType.LIMIT, price="100", stop="90"))
        gw.advance()
        assert gw.fills == ()
        assert (await gw.get_order(SYMBOL, "e1")).status is OrderStatus.NEW
        gw.advance()
        [fill] = gw.fills
        assert fill.kind is FillKind.ENTRY_LIMIT
        assert fill.liquidity is Liquidity.MAKER

    async def test_limit_buy_fills_at_limit_price_even_with_favorable_gap(self) -> None:
        candles = [*flat(1), candle(1, "95", "99", "94", "98")]
        gw = make(candles, slippage_bps=D(50))
        gw.advance()
        await gw.place_order(entry(kind=OrderType.LIMIT, price="100", stop="80", target="150"))
        gw.advance()
        [fill] = gw.fills
        assert fill.price == D(100)
        assert fill.fee == D("0.1")
        assert fill.slippage_cost == 0

    async def test_limit_buy_touching_price_exactly_fills(self) -> None:
        gw = make([*flat(1), candle(1, "105", "106", "100", "103")])
        gw.advance()
        await gw.place_order(entry(kind=OrderType.LIMIT, price="100", stop="90"))
        gw.advance()
        assert len(gw.fills) == 1

    async def test_limit_sell_requires_high_to_reach_price(self) -> None:
        candles = [
            *flat(2),
            candle(2, "100", "104", "99", "100"),
            candle(3, "100", "110", "99", "100"),
        ]
        gw = make(candles, maker_fee=D("0.0005"))
        await gw.place_order(entry(stop="50", target="200"))
        gw.advance()
        gw.advance()
        await gw.place_order(sell(kind=OrderType.LIMIT, price="110"))
        gw.advance()
        assert len(gw.fills) == 1
        gw.advance()
        fill = gw.fills[-1]
        assert fill.kind is FillKind.EXIT_LIMIT
        assert fill.price == D(110)
        assert fill.fee == D("0.055")

    async def test_cancel_releases_reservation(self) -> None:
        gw = make(flat(3), initial_quote=D(150))
        await gw.place_order(entry("a", kind=OrderType.LIMIT, price="100", stop="90"))
        assert (await gw.get_balances())["USDT"].locked > 0
        report = await gw.cancel_order(SYMBOL, "a")
        assert report.status is OrderStatus.CANCELED
        assert (await gw.get_balances())["USDT"].locked == 0
        gw.advance()
        assert gw.fills == ()
        again = await gw.cancel_order(SYMBOL, "a")
        assert again.status is OrderStatus.CANCELED


class TestOco:
    async def _open(self, candles: list[Candle], **config: Any) -> BacktestGateway:
        gw = make(candles, **config)
        await gw.place_order(entry(stop="95", target="110"))
        gw.advance()
        return gw

    async def test_stop_hit(self) -> None:
        gw = await self._open([*flat(1), candle(1, "100", "102", "94", "96")])
        gw.advance()
        fill = gw.fills[-1]
        assert fill.kind is FillKind.STOP_LOSS
        assert fill.price == D(95)
        assert fill.exit_reason is ExitReason.STOP_LOSS
        assert fill.liquidity is Liquidity.TAKER
        assert (await gw.get_order(SYMBOL, "e1:oco")).status is OrderStatus.FILLED

    async def test_target_hit_is_maker_at_target(self) -> None:
        gw = await self._open([*flat(1), candle(1, "100", "112", "99", "111")], slippage_bps=D(50))
        gw.advance()
        fill = gw.fills[-1]
        assert fill.kind is FillKind.TAKE_PROFIT
        assert fill.price == D(110)
        assert fill.liquidity is Liquidity.MAKER
        assert fill.slippage_cost == 0
        usdt, btc = await balances(gw)
        assert btc == 0
        entry_fill = gw.fills[0]
        assert usdt == D(10_000) - entry_fill.price - entry_fill.fee + D(110) - fill.fee

    async def test_stop_and_target_same_candle_assumes_stop(self) -> None:
        gw = await self._open([*flat(1), candle(1, "100", "115", "90", "100")])
        gw.advance()
        assert gw.fills[-1].kind is FillKind.STOP_LOSS

    async def test_gap_below_stop_fills_at_open(self) -> None:
        gw = await self._open([*flat(1), candle(1, "80", "85", "78", "82")], slippage_bps=D(100))
        gw.advance()
        fill = gw.fills[-1]
        assert fill.price == D(80) * D("0.99")
        assert fill.at == T0 + timedelta(hours=1)

    async def test_slippage_applies_to_stop(self) -> None:
        gw = await self._open([*flat(1), candle(1, "100", "101", "90", "92")], slippage_bps=D(100))
        gw.advance()
        fill = gw.fills[-1]
        assert fill.price == D(95) * D("0.99")
        assert fill.slippage_cost == D("0.95")

    async def test_stop_checked_on_entry_candle_but_target_is_not(self) -> None:
        gw = make([flat(1)[0], candle(1, "100", "120", "99", "118")])
        gw.advance()
        await gw.place_order(entry(stop="95", target="110"))
        gw.advance()
        assert [f.kind for f in gw.fills] == [FillKind.ENTRY_MARKET]
        gw2 = make([flat(1)[0], candle(1, "100", "120", "90", "118")])
        gw2.advance()
        await gw2.place_order(entry(stop="95", target="110"))
        gw2.advance()
        assert [f.kind for f in gw2.fills] == [FillKind.ENTRY_MARKET, FillKind.STOP_LOSS]

    async def test_raise_stop_moves_protection_and_never_lowers(self) -> None:
        candles = [*flat(2), candle(2, "100", "104", "97", "102")]
        gw = make(candles)
        await gw.place_order(entry(stop="90", target="130"))
        gw.advance()
        gw.advance()
        gw.raise_stop("e1", D(98))
        with pytest.raises(ValueError, match="raised"):
            gw.raise_stop("e1", D(97))
        with pytest.raises(ValueError, match="below"):
            gw.raise_stop("e1", D(100))
        gw.advance()
        assert gw.fills[-1].price == D(98)

    def test_raise_stop_unknown(self) -> None:
        gw = make(flat(2))
        with pytest.raises(UnknownOrderError):
            gw.raise_stop("nope", D(1))

    async def test_cancel_oco_leaves_position_unprotected(self) -> None:
        gw = await self._open([*flat(1), candle(1, "100", "101", "50", "60")])
        report = await gw.cancel_order(SYMBOL, "e1:oco")
        assert report.status is OrderStatus.CANCELED
        gw.advance()
        assert len(gw.fills) == 1
        assert (await balances(gw))[1] == 1

    async def test_partial_exit_shrinks_oco(self) -> None:
        candles = [*flat(3), candle(3, "100", "125", "99", "100")]
        gw = make(candles)
        await gw.place_order(entry(qty="2", stop="90", target="120"))
        gw.advance()
        await gw.place_order(sell(qty="1"))
        gw.advance()
        gw.advance()
        gw.advance()
        assert [f.quantity for f in gw.fills] == [2, 1, 1]
        assert gw.fills[-1].kind is FillKind.TAKE_PROFIT
        assert (await balances(gw))[1] == 0


class TestBalances:
    async def test_insufficient_balance_rejected_without_state_change(self) -> None:
        gw = make(flat(3), initial_quote=D(99))
        report = await gw.place_order(entry())
        assert report.status is OrderStatus.REJECTED
        assert gw.rejection_reason("e1") == "insufficient quote balance"
        gw.advance()
        assert gw.fills == ()
        assert (await balances(gw)) == (D(99), D(0))

    async def test_fees_count_in_required_balance(self) -> None:
        gw = make(flat(3), initial_quote=D(100))
        assert (await gw.place_order(entry(kind=OrderType.LIMIT))).status is OrderStatus.REJECTED
        gw2 = make(flat(3), initial_quote=D("100.1"))
        assert (await gw2.place_order(entry(kind=OrderType.LIMIT))).status is OrderStatus.NEW

    async def test_pending_orders_reserve_balance(self) -> None:
        gw = make(flat(3), initial_quote=D(150))
        assert (await gw.place_order(entry("a", kind=OrderType.LIMIT))).status is OrderStatus.NEW
        assert (await gw.place_order(entry("b", kind=OrderType.LIMIT))).status is OrderStatus.REJECTED
        assert (await gw.get_balances())["USDT"].free < 50

    async def test_gap_up_market_entry_rejected_when_funds_run_out(self) -> None:
        gw = make([flat(1)[0], candle(1, "130", "130", "130", "130")], initial_quote=D(110))
        gw.advance()
        assert (await gw.place_order(entry(stop="90", target="200"))).status is OrderStatus.NEW
        gw.advance()
        assert (await gw.get_order(SYMBOL, "e1")).status is OrderStatus.REJECTED
        assert gw.fills == ()
        assert (await balances(gw)) == (D(110), D(0))

    async def test_cannot_sell_without_position_no_short(self) -> None:
        gw = make(flat(2))
        report = await gw.place_order(sell())
        assert report.status is OrderStatus.REJECTED
        assert gw.rejection_reason("x1") == "insufficient base balance"

    async def test_cannot_oversell(self) -> None:
        gw = make(flat(3))
        await gw.place_order(entry(stop="50", target="200"))
        gw.advance()
        assert (await gw.place_order(sell("s1", qty="1"))).status is OrderStatus.NEW
        assert (await gw.place_order(sell("s2", qty="1"))).status is OrderStatus.REJECTED

    async def test_wrong_symbol_rejected(self) -> None:
        gw = make(flat(2))
        other = entry().model_copy(update={"symbol": "ETH/USDT"})
        assert (await gw.place_order(other)).status is OrderStatus.REJECTED

    async def test_initial_balances(self) -> None:
        gw = make(flat(2), initial_quote=D(500))
        b = await gw.get_balances()
        assert b["USDT"].free == 500
        assert b["BTC"].free == 0

    async def test_duplicate_and_unknown_ids(self) -> None:
        gw = make(flat(2))
        await gw.place_order(entry())
        with pytest.raises(DuplicateOrderError):
            await gw.place_order(entry())
        with pytest.raises(UnknownOrderError):
            await gw.get_order(SYMBOL, "zzz")
        with pytest.raises(UnknownOrderError):
            await gw.cancel_order(SYMBOL, "zzz:oco")

    async def test_tampered_order_never_registered(self) -> None:
        gw = make(flat(2))
        order = entry()
        object.__setattr__(order, "stop_loss", D(200))
        with pytest.raises(ValueError, match="stop_loss"):
            await gw.place_order(order)
        assert gw.submitted_orders == []


class TestFillsReportAndDeterminism:
    async def _run(self) -> tuple[Any, ...]:
        candles = [
            candle(0, "100", "101", "99", "100"),
            candle(1, "100", "104", "98", "103"),
            candle(2, "103", "112", "102", "111"),
            candle(3, "111", "112", "105", "106"),
        ]
        gw = make(candles, slippage_bps=D(5))
        await gw.place_order(entry(stop="95", target="110"))
        async for _ in gw.stream_candles(SYMBOL, TF):
            pass
        return gw.fills

    async def test_same_input_same_fills(self) -> None:
        assert await self._run() == await self._run()

    async def test_fills_are_immutable_snapshot_with_sequence(self) -> None:
        fills = await self._run()
        assert isinstance(fills, tuple)
        assert [f.sequence for f in fills] == [1, 2]
        with pytest.raises(ValueError, match="frozen"):
            fills[0].price = D(1)

    async def test_fills_snapshot_does_not_change_later(self) -> None:
        gw = make(flat(3))
        await gw.place_order(entry(stop="50", target="200"))
        before = gw.fills
        gw.advance()
        assert before == ()
        assert len(gw.fills) == 1

    async def test_no_lookahead_future_does_not_affect_past_fills(self) -> None:
        base = [candle(0, "100", "100", "100", "100"), candle(1, "100", "100", "100", "100")]
        a = make([*base, candle(2, "100", "100", "1", "50")])
        b = make([*base, candle(2, "100", "500", "100", "400")])
        for gw in (a, b):
            await gw.place_order(entry(stop="95", target="110"))
            gw.advance()
            gw.advance()
        assert a.fills == b.fills
