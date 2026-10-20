"""Evidencias da revisao QA do PaperGateway (xfail strict = bug confirmado)."""

from datetime import datetime, timedelta
from decimal import Decimal as D

import pytest

from engine.domain.order import OrderStatus, OrderType
from engine.domain.types import Timeframe
from engine.infrastructure.backtest import FillKind, Liquidity
from engine.infrastructure.paper import MarketDataStalledError, PaperConfig, PaperGateway
from tests.unit.paper.helpers import T0, FakeMarket, book, candle, entry, make, opened, sell


class TickingClock:
    """Relogio real simulado: cada leitura avanca 1 ms (como o SystemClock entre duas chamadas)."""

    def __init__(self) -> None:
        self._now = T0

    def now(self) -> datetime:
        self._now += timedelta(milliseconds=1)
        return self._now

    def jump(self, seconds: int) -> None:
        self._now += timedelta(seconds=seconds)


async def test_replayed_candles_after_a_limit_fill_still_evaluate_the_stop() -> None:
    gateway, clock = make()
    await gateway.place_order(entry("e1", kind=OrderType.LIMIT, price="100", stop="90", target="120"))
    clock.advance(seconds=86_400)  # processo ficou fora do ar; velas historicas chegam agora
    gateway.on_candle(candle(1, "101", "102", "99", "100"))  # enche a entrada
    gateway.on_candle(candle(2, "100", "100", "80", "85"))  # logo depois rompe o stop (vela historica)
    assert gateway.fills[-1].kind is FillKind.STOP_LOSS


async def test_stop_is_active_in_the_fill_tick_with_a_real_ticking_clock() -> None:
    clock = TickingClock()
    gateway = PaperGateway(FakeMarket(), clock, PaperConfig(slippage_bps=D(0)))
    gateway.on_book(book("85", "100"))
    await gateway.place_order(entry("e1", stop="90", target="120"))
    clock.jump(1)
    gateway.on_book(book("85", "100"))  # entrada enche ao ask 100 com bid 85 <= stop 90
    assert gateway.fills[-1].kind is FillKind.STOP_LOSS


async def test_cancel_of_the_oco_leaves_the_position_unprotected_by_design() -> None:
    gateway, _ = await opened()
    report = await gateway.cancel_order("BTC/USDT", "e1:oco")
    assert report.status is OrderStatus.CANCELED
    gateway.on_book(book("50", "51"))
    assert len(gateway.fills) == 1  # sem stop: a posicao segue aberta com preco muito abaixo do stop


async def test_restore_round_trip_is_deterministic_after_gap() -> None:
    gateway, clock = await opened()
    state = gateway.snapshot()
    a = PaperGateway.restore(PaperGateway.restore(state, FakeMarket(), clock).snapshot(), FakeMarket(), clock)
    assert a.snapshot() == state
    clock.advance(seconds=3600)
    a.on_book(book("60", "61"))
    assert a.fills[-1].kind is FillKind.STOP_LOSS
    assert a.fills[-1].price == D(60)


class TestBoundaries:
    async def test_limit_entry_fills_when_ask_equals_the_limit(self) -> None:
        gateway, clock = make()
        gateway.on_book(book("99", "101"))
        await gateway.place_order(entry("e1", kind=OrderType.LIMIT, price="100"))
        clock.advance(ms=100)
        gateway.on_book(book("99", "100"))
        assert gateway.fills[-1].kind is FillKind.ENTRY_LIMIT

    async def test_limit_exit_fills_when_bid_equals_the_limit(self) -> None:
        gateway, clock = await opened()
        await gateway.place_order(sell("x1", kind=OrderType.LIMIT, qty="0.5", price="110"))
        clock.advance(ms=100)
        gateway.on_book(book("110", "111"))
        assert gateway.fills[-1].kind is FillKind.EXIT_LIMIT

    async def test_book_is_fresh_at_exactly_max_age_and_stale_after(self) -> None:
        gateway, clock = make(max_book_age_seconds=10)
        gateway.on_book(book("99", "100"))
        clock.advance(seconds=10)
        assert gateway.best_prices("BTC/USDT") is not None
        clock.advance(ms=1)
        assert gateway.best_prices("BTC/USDT") is None

    async def test_unclosed_candle_never_triggers_the_stop(self) -> None:
        gateway, clock = await opened()
        clock.advance(seconds=120)
        gateway.on_candle(candle(1, "100", "101", "80", "99").model_copy(update={"closed": False}))
        assert len(gateway.fills) == 1

    async def test_entry_rejected_when_the_fill_price_jumped_beyond_the_balance(self) -> None:
        gateway, clock = make(initial_quote=D(150))
        gateway.on_book(book("99", "100"))
        report = await gateway.place_order(entry("e1", stop="90", target="120"))
        assert report.status is OrderStatus.NEW
        clock.advance(ms=100)
        gateway.on_book(book("199", "200"))
        assert (await gateway.get_order("BTC/USDT", "e1")).status is OrderStatus.REJECTED
        assert (await gateway.get_balances())["USDT"].free == D(150)

    async def test_limit_entry_expires_exactly_at_the_ttl(self) -> None:
        gateway, clock = make(entry_limit_ttl_seconds=5)
        gateway.on_book(book("99", "101"))
        await gateway.place_order(entry("e1", kind=OrderType.LIMIT, price="100"))
        clock.advance(seconds=5)
        gateway.on_book(book("99", "101"))
        assert (await gateway.get_order("BTC/USDT", "e1")).status is OrderStatus.EXPIRED


class TestStopBlindness:
    async def test_stops_blind_after_the_feed_goes_quiet_with_an_active_oco(self) -> None:
        gateway, clock = await opened(max_stop_blind_seconds=30)
        assert not gateway.stops_blind
        clock.advance(seconds=31)
        assert gateway.stops_blind
        assert gateway.stale_since()["BTC/USDT"] == timedelta(seconds=31)
        gateway.on_book(book("99", "100"))
        assert not gateway.health().stops_blind

    async def test_no_blindness_without_active_oco(self) -> None:
        gateway, clock = make(max_stop_blind_seconds=30)
        clock.advance(seconds=300)
        assert gateway.health().stale == {}
        assert not gateway.stops_blind

    async def test_open_protective_state_lists_active_ocos_only(self) -> None:
        gateway, _ = await opened()
        [oco] = gateway.open_protective_state()
        assert (oco.symbol, oco.quantity, oco.stop) == ("BTC/USDT", D(1), D(90))
        await gateway.cancel_order("BTC/USDT", "e1:oco")
        assert gateway.open_protective_state() == []

    async def test_run_raises_when_a_book_stream_ends(self) -> None:
        gateway, _ = make(FakeMarket())
        with pytest.raises(ExceptionGroup) as raised:
            await gateway.run()
        assert raised.group_contains(MarketDataStalledError)


class TestReviewFixes:
    async def test_same_candle_that_fills_the_limit_and_breaks_the_stop_is_pessimistic(self) -> None:
        gateway, clock = make()
        await gateway.place_order(entry("e1", kind=OrderType.LIMIT, price="100", stop="90"))
        clock.advance(seconds=60)
        gateway.on_candle(candle(1, "101", "102", "85", "95"))
        assert [f.kind for f in gateway.fills] == [FillKind.ENTRY_LIMIT, FillKind.STOP_LOSS]

    async def test_candle_fill_uses_market_time(self) -> None:
        gateway, clock = make()
        await gateway.place_order(entry("e1", kind=OrderType.LIMIT, price="100"))
        clock.advance(seconds=86_400)
        c = candle(1, "101", "102", "99", "100")
        gateway.on_candle(c)
        assert gateway.fills[0].at == c.close_time

    async def test_candle_dedupe_is_per_timeframe(self) -> None:
        gateway, _ = make()
        gateway.on_candle(candle(5, "100", "101", "99", "100"))
        other = candle(1, "100", "101", "99", "100").model_copy(update={"timeframe": Timeframe.M5})
        gateway.on_candle(other)
        assert len(gateway.snapshot().last_candle_open) == 2

    async def test_marketable_limit_pays_taker_fee_at_the_book(self) -> None:
        gateway, clock = make(taker_fee=D("0.002"), maker_fee=D("0.001"))
        gateway.on_book(book("98", "99"))
        await gateway.place_order(entry("e1", kind=OrderType.LIMIT, price="100"))
        clock.advance(ms=100)
        gateway.on_book(book("98", "99"))
        fill = gateway.fills[0]
        assert (fill.liquidity, fill.price, fill.fee) == (Liquidity.TAKER, D(99), D("0.198"))

    async def test_thin_book_market_exit_applies_extra_slippage_to_the_remainder(self) -> None:
        gateway, clock = await opened(thin_book_slippage_bps=D(100))
        await gateway.place_order(sell("x1", qty="1"))
        clock.advance(ms=100)
        gateway.on_book(book("0", "0", bids=[("100", "0.5")], asks=[("101", "10")]))
        assert gateway.fills[-1].price == (D("50") + D("0.5") * D(100) * D("0.99")) / D(1)

    async def test_restore_with_a_different_config_requires_the_override_flag(self) -> None:
        gateway, clock = await opened()
        state = gateway.snapshot()
        other = state.config.model_copy(update={"taker_fee": D("0.0005")})
        with pytest.raises(ValueError, match="allow_config_override"):
            PaperGateway.restore(state, FakeMarket(), clock, other)
