from collections.abc import AsyncIterator, Iterable
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from typing import Any

from engine.domain.candle import Candle
from engine.domain.market import BookLevel, OrderBookSnapshot
from engine.domain.order import EntryOrder, ExitOrder, OrderType
from engine.domain.types import Symbol, Timeframe
from engine.infrastructure.paper import PaperConfig, PaperGateway

T0 = datetime(2026, 1, 1, tzinfo=UTC)
SYMBOL = "BTC/USDT"


class FakeClock:
    def __init__(self, start: datetime = T0) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, ms: int = 0, seconds: int = 0) -> None:
        self._now += timedelta(milliseconds=ms, seconds=seconds)


class FakeMarket:
    """Fonte de mercado fake: reproduz listas pre-carregadas e termina."""

    def __init__(self, books: Iterable[OrderBookSnapshot] = (), candles: Iterable[Candle] = ()) -> None:
        self.books = list(books)
        self.candles = list(candles)

    async def fetch_candles(
        self, symbol: Symbol, timeframe: Timeframe, start: datetime, end: datetime | None = None
    ) -> list[Candle]:
        return [c for c in self.candles if c.open_time >= start]

    async def stream_candles(
        self, symbol: Symbol, timeframe: Timeframe, since: datetime | None = None
    ) -> AsyncIterator[Candle]:
        for candle in self.candles:
            yield candle

    async def stream_order_book(self, symbol: Symbol, depth: int = 10) -> AsyncIterator[OrderBookSnapshot]:
        for book in self.books:
            yield book


def book(
    bid: str | float,
    ask: str | float,
    *,
    bids: list[tuple[str, str]] | None = None,
    asks: list[tuple[str, str]] | None = None,
    symbol: str = SYMBOL,
) -> OrderBookSnapshot:
    bid_levels = bids or [(str(bid), "10")]
    ask_levels = asks or [(str(ask), "10")]
    return OrderBookSnapshot(
        symbol=symbol,
        bids=tuple(BookLevel(price=D(p), quantity=D(q)) for p, q in bid_levels),
        asks=tuple(BookLevel(price=D(p), quantity=D(q)) for p, q in ask_levels),
        received_at=T0,
    )


def candle(i: int, o: str, h: str, low: str, c: str, *, base: datetime = T0) -> Candle:
    return Candle(
        symbol=SYMBOL,
        timeframe=Timeframe.M1,
        open_time=base + timedelta(minutes=i),
        open=D(o),
        high=D(h),
        low=D(low),
        close=D(c),
        volume=D(1),
        closed=True,
    )


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


def make(
    market: FakeMarket | None = None, clock: FakeClock | None = None, **config: Any
) -> tuple[PaperGateway, FakeClock]:
    clock = clock or FakeClock()
    settings: dict[str, Any] = {"slippage_bps": D(0), "latency_ms": 100, **config}
    return PaperGateway(market or FakeMarket(), clock, PaperConfig(**settings)), clock


async def opened(
    *,
    stop: str = "90",
    target: str = "120",
    qty: str = "1",
    ask: str = "100",
    **config: Any,
) -> tuple[PaperGateway, FakeClock]:
    """Gateway com uma entrada a mercado ja executada (OCO armado)."""
    gateway, clock = make(**config)
    gateway.on_book(book("99", ask))
    await gateway.place_order(entry(stop=stop, target=target, qty=qty))
    clock.advance(ms=100)
    gateway.on_book(book("99", ask))
    return gateway, clock
