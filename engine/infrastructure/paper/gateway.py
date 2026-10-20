import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal, Self

from engine.domain.candle import Candle
from engine.domain.gateway import DuplicateOrderError, MarketDataGateway, UnknownOrderError
from engine.domain.market import Balance, OrderBookSnapshot
from engine.domain.order import (
    EntryOrder,
    ExitOrder,
    Order,
    OrderReport,
    OrderStatus,
    OrderType,
    revalidate_order,
)
from engine.domain.trade import ExitReason
from engine.domain.types import Symbol, Timeframe
from engine.infrastructure.backtest.fills import Fill, FillKind, Liquidity
from engine.infrastructure.paper.book import sweep
from engine.infrastructure.paper.clock import Clock, SystemClock
from engine.infrastructure.paper.config import PaperConfig
from engine.infrastructure.paper.state import PaperOcoState, PaperOrderState, PaperPosition, PaperState

_BPS = Decimal(10_000)
_OCO_SUFFIX = ":oco"
_ZERO = Decimal(0)
_KEEP_FILLS = 20
_KEEP_RETIRED_IDS = 500


class MarketDataStalledError(RuntimeError):
    """O fluxo de mercado de um simbolo terminou: os stops ficariam cegos em silencio."""


@dataclass(frozen=True)
class PaperHealth:
    """`stale`: idade do ultimo book/vela por simbolo com OCO; `stops_blind`: alguma passou do limite"""

    stale: dict[Symbol, timedelta]
    stops_blind: bool


@dataclass
class _Working:
    order: EntryOrder | ExitOrder
    order_id: str
    submitted_at: datetime
    executable_at: datetime
    status: OrderStatus = OrderStatus.NEW
    filled: Decimal = _ZERO
    average_price: Decimal | None = None
    reserved_quote: Decimal = _ZERO
    rejection: str | None = None
    marketable: bool = False


@dataclass
class _Oco:
    entry_id: str
    order_id: str
    symbol: Symbol
    quantity: Decimal
    stop: Decimal
    target: Decimal | None
    armed_at: datetime
    status: OrderStatus = OrderStatus.NEW
    filled: Decimal = _ZERO
    average_price: Decimal | None = None

    @property
    def client_order_id(self) -> str:
        return f"{self.entry_id}{_OCO_SUFFIX}"


@dataclass
class _Position:
    quantity: Decimal = _ZERO
    average_price: Decimal = _ZERO
    opened_at: datetime | None = None
    realized_pnl: Decimal = _ZERO
    fees_paid: Decimal = _ZERO


@dataclass(frozen=True)
class _Tick:
    """Leitura de mercado: top-of-book (low == high) ou intervalo de uma vela fechada (sem book)."""

    symbol: Symbol
    window_start: datetime
    at: datetime
    low_bid: Decimal
    high_bid: Decimal
    low_ask: Decimal
    book: OrderBookSnapshot | None = None
    candle_open: Decimal | None = None


@dataclass(frozen=True)
class _Execution:
    kind: FillKind
    liquidity: Liquidity
    fee_rate: Decimal
    price: Decimal
    slippage: Decimal


class PaperGateway:
    """ExchangeGateway com mercado real e execucao simulada (nunca envia ordens a corretora).

    Tempo: fills por book usam o relogio; fills por vela usam o tempo de mercado da vela (`close_time`),
    para que o replay historico apos uma queda avalie as velas seguintes contra o OCO recem-armado.
    Anti-lookahead (otimista): a vela do fill e descartada para o stop, exceto no pessimismo abaixo.
    Entrada limit tocada por vela cuja minima ja rompe o stop: stop executa na propria vela (pessimista).
    Limit "marketable" (ja cruza o book ao ser enviado) executa ao book como taker, nunca acima do limite.

    O estado so muda por `on_book`/`on_candle` (alimentados por `run()` e por `stream_candles`) e pelos
    metodos de ordem. Pessimismo: limit executa no preco limite; stop e alvo no mesmo tick/intervalo => stop;
    limit de venda no mesmo tick de um stop nao executa; gap abaixo do stop executa no book corrente;
    slippage sempre contra o trader; a ordem so vira executavel apos a latencia; sem book nao ha preco de
    mercado (rejeita). O stop do OCO vale desde o fill da entrada e e avaliado a cada atualizacao.
    """

    def __init__(
        self, market: MarketDataGateway, clock: Clock | None = None, config: PaperConfig | None = None
    ) -> None:
        self._market = market
        self._clock: Clock = clock or SystemClock()
        self._config = config or PaperConfig()
        self._quote = self._config.quote_asset
        self._balances: dict[str, Decimal] = {self._quote: self._config.initial_quote}
        for symbol in self._config.symbols:
            self._balances.setdefault(_base(symbol), _ZERO)
        self._orders: dict[str, _Working] = {}
        self._ocos: dict[str, _Oco] = {}
        self._positions: dict[Symbol, _Position] = {}
        self._fills: list[Fill] = []
        self._books: dict[Symbol, tuple[OrderBookSnapshot, datetime]] = {}
        self._last_candle_open: dict[str, datetime] = {}
        self._order_seq = 0
        self._fill_seq = 0
        self._retired_ids: dict[str, None] = {}
        self._last_update_at: datetime | None = None
        self._last_feed: dict[Symbol, datetime] = {}
        self._created_at = self._clock.now()

    @classmethod
    def restore(
        cls,
        state: PaperState,
        market: MarketDataGateway,
        clock: Clock | None = None,
        config: PaperConfig | None = None,
        *,
        allow_config_override: bool = False,
    ) -> Self:
        """Reconstroi o gateway sem o book (preco velho): a primeira atualizacao reavalia tudo.

        Aceita snapshot completo ou compacto (`snapshot(compact=True)`).

        Config diferente da do snapshot exige `allow_config_override=True`.
        """
        if config is not None and config != state.config and not allow_config_override:
            raise ValueError("config differs from the snapshot; pass allow_config_override=True")
        gateway = cls(market, clock, config or state.config)
        gateway._balances = dict(state.balances)
        gateway._balances.setdefault(gateway._quote, _ZERO)
        for symbol in gateway._config.symbols:
            gateway._balances.setdefault(_base(symbol), _ZERO)
        gateway._order_seq = state.order_seq
        gateway._fill_seq = max(state.fill_seq, state.fills[-1].sequence if state.fills else 0)
        gateway._retired_ids = dict.fromkeys(state.retired_order_ids)
        gateway._last_update_at = state.last_update_at
        gateway._last_candle_open = dict(state.last_candle_open)
        gateway._fills = list(state.fills)
        for o in state.orders:
            gateway._orders[o.order.client_order_id] = _Working(
                o.order,
                o.order_id,
                o.submitted_at,
                o.executable_at,
                o.status,
                o.filled_quantity,
                o.average_price,
                o.reserved_quote,
                o.rejection,
                o.marketable,
            )
        for c in state.ocos:
            oco = _Oco(
                c.entry_client_order_id,
                c.order_id,
                c.symbol,
                c.quantity,
                c.stop,
                c.target,
                c.armed_at,
                c.status,
                c.filled_quantity,
                c.average_price,
            )
            gateway._ocos[oco.client_order_id] = oco
        for p in state.positions:
            gateway._positions[p.symbol] = _Position(
                p.quantity, p.average_price, p.opened_at, p.realized_pnl, p.fees_paid
            )
        return gateway

    def snapshot(self, *, compact: bool = True) -> PaperState:
        """`compact` descarta ordens/OCOs terminais e fills antigos (ja persistidos no diario).

        Ficam: saldos, ordens abertas, OCOs ativos, posicoes, contadores, cursor de velas, os ultimos
        `_KEEP_FILLS` fills e os ids de ordens encerradas (limitados) para rejeitar duplicatas.
        """
        orders = [w for w in self._orders.values() if not compact or w.status is OrderStatus.NEW]
        retired = dict(self._retired_ids)
        if compact:
            retired.update(
                dict.fromkeys(i for i, w in self._orders.items() if w.status is not OrderStatus.NEW)
            )
        fills = self._fills[-_KEEP_FILLS:] if compact else self._fills
        return PaperState(
            config=self._config,
            balances=dict(self._balances),
            orders=tuple(
                PaperOrderState(
                    order=w.order,
                    order_id=w.order_id,
                    status=w.status,
                    filled_quantity=w.filled,
                    average_price=w.average_price,
                    reserved_quote=w.reserved_quote,
                    rejection=w.rejection,
                    marketable=w.marketable,
                    submitted_at=w.submitted_at,
                    executable_at=w.executable_at,
                )
                for w in orders
            ),
            ocos=tuple(
                PaperOcoState(
                    entry_client_order_id=o.entry_id,
                    order_id=o.order_id,
                    symbol=o.symbol,
                    quantity=o.quantity,
                    stop=o.stop,
                    target=o.target,
                    armed_at=o.armed_at,
                    status=o.status,
                    filled_quantity=o.filled,
                    average_price=o.average_price,
                )
                for o in self._ocos.values()
                if not compact or o.status is OrderStatus.NEW
            ),
            positions=tuple(_position_state(symbol, p) for symbol, p in self._positions.items()),
            fills=tuple(fills),
            fill_seq=self._fill_seq,
            retired_order_ids=tuple(list(retired)[-_KEEP_RETIRED_IDS:]),
            order_seq=self._order_seq,
            last_candle_open=dict(self._last_candle_open),
            last_update_at=self._last_update_at,
        )

    @property
    def config(self) -> PaperConfig:
        return self._config

    @property
    def fills(self) -> tuple[Fill, ...]:
        return tuple(self._fills)

    @property
    def positions(self) -> dict[Symbol, PaperPosition]:
        return {s: _position_state(s, p) for s, p in self._positions.items() if p.quantity > 0}

    @property
    def last_update_at(self) -> datetime | None:
        return self._last_update_at

    @property
    def stops_blind(self) -> bool:
        return self.health().stops_blind

    def open_protective_state(self) -> list[PaperOcoState]:
        """OCOs ativos (simbolo/quantidade/stop/alvo) para reconciliacao e 'nenhuma posicao sem stop'."""
        return [state for state in self.snapshot(compact=True).ocos if state.status is OrderStatus.NEW]

    def stale_since(self, now: datetime | None = None) -> dict[Symbol, timedelta]:
        """Ha quanto tempo nao chega book/vela, por simbolo com OCO ativo."""
        now = now or self._clock.now()
        symbols = {o.symbol for o in self._ocos.values() if o.status is OrderStatus.NEW}
        return {s: now - self._last_feed.get(s, self._created_at) for s in sorted(symbols)}

    def health(self, now: datetime | None = None) -> PaperHealth:
        stale = self.stale_since(now)
        limit = timedelta(seconds=self._config.max_stop_blind_seconds)
        return PaperHealth(stale, any(age > limit for age in stale.values()))

    @property
    def submitted_orders(self) -> list[Order]:
        return [w.order for w in self._orders.values()]

    def rejection_reason(self, client_order_id: str) -> str | None:
        return self._working(client_order_id).rejection

    def best_prices(self, symbol: Symbol) -> tuple[Decimal, Decimal] | None:
        """(bid, ask) do ultimo book, se ainda fresco."""
        book = self._fresh_book(symbol)
        return None if book is None else (book.bids[0].price, book.asks[0].price)

    async def run(self, on_stops_blind: Callable[[PaperHealth], None] | None = None) -> None:
        """Consome o book real de cada simbolo ate ser cancelada; nunca termina em silencio.

        Fluxo encerrado => `MarketDataStalledError` (dentro de um ExceptionGroup, derrubando os demais).
        `on_stops_blind` e chamado pelo watchdog a cada transicao para "stops cegos" (e ao voltar).
        """
        async with asyncio.TaskGroup() as group:
            for symbol in self._config.symbols:
                group.create_task(self._follow_book(symbol))
            if on_stops_blind is not None:
                group.create_task(self.watchdog(on_stops_blind))

    async def watchdog(self, on_change: Callable[[PaperHealth], None]) -> None:
        interval = max(self._config.max_stop_blind_seconds / 4, 0.05)
        blind = False
        while True:
            health = self.health()
            if health.stops_blind != blind:
                blind = health.stops_blind
                on_change(health)
            await asyncio.sleep(interval)

    def on_book(self, book: OrderBookSnapshot) -> None:
        if book.symbol not in self._config.symbols or not book.bids or not book.asks:
            return
        now = self._clock.now()
        self._books[book.symbol] = (book, now)
        self._last_feed[book.symbol] = now
        bid, ask = book.bids[0].price, book.asks[0].price
        self._process(_Tick(book.symbol, now, now, bid, bid, ask, book), now)

    def on_candle(self, candle: Candle) -> None:
        """Vela fechada: cobre toques de preco entre books (queda de conexao); nunca executa a mercado."""
        if not candle.closed or candle.symbol not in self._config.symbols:
            return
        now = self._clock.now()
        self._last_feed[candle.symbol] = now
        key = f"{candle.symbol}|{candle.timeframe}"
        last = self._last_candle_open.get(key)
        if last is not None and candle.open_time <= last:
            return
        self._last_candle_open[key] = candle.open_time
        tick = _Tick(
            candle.symbol,
            candle.open_time,
            candle.close_time,
            candle.low,
            candle.high,
            candle.low,
            None,
            candle.open,
        )
        self._process(tick, now)

    async def fetch_candles(
        self, symbol: Symbol, timeframe: Timeframe, start: datetime, end: datetime | None = None
    ) -> list[Candle]:
        return await self._market.fetch_candles(symbol, timeframe, start, end)

    async def stream_candles(
        self, symbol: Symbol, timeframe: Timeframe, since: datetime | None = None
    ) -> AsyncIterator[Candle]:
        """Repassa as velas da fonte; cada uma alimenta a simulacao antes de chegar ao consumidor."""
        async for candle in _closing(self._market.stream_candles(symbol, timeframe, since)):
            self.on_candle(candle)
            yield candle

    def stream_order_book(self, symbol: Symbol, depth: int = 10) -> AsyncIterator[OrderBookSnapshot]:
        return self._market.stream_order_book(symbol, depth)

    async def get_balances(self) -> dict[str, Balance]:
        quote_locked = self._reserved_quote()
        result = {
            self._quote: Balance(
                asset=self._quote, free=self._balances[self._quote] - quote_locked, locked=quote_locked
            )
        }
        for symbol in self._config.symbols:
            asset = _base(symbol)
            total = self._balances[asset]
            locked = min(total, self._protected(symbol) + self._pending_exit_quantity(symbol))
            result[asset] = Balance(asset=asset, free=total - locked, locked=locked)
        return result

    async def place_order(self, order: Order) -> OrderReport:
        checked = revalidate_order(order)
        if checked.client_order_id in self._orders or checked.client_order_id in self._retired_ids:
            raise DuplicateOrderError(checked.client_order_id)
        if checked.client_order_id.endswith(_OCO_SUFFIX):
            raise ValueError(f"client_order_id must not end with {_OCO_SUFFIX!r}")
        now = self._clock.now()
        self._order_seq += 1
        working = _Working(
            order=checked,
            order_id=f"paper-{self._order_seq}",
            submitted_at=now,
            executable_at=now + timedelta(milliseconds=self._config.latency_ms),
        )
        self._orders[checked.client_order_id] = working
        if checked.symbol not in self._config.symbols:
            return self._reject(working, "unknown symbol")
        if checked.type is OrderType.MARKET and self._fresh_book(checked.symbol) is None:
            return self._reject(working, "no order book available")
        match checked:
            case EntryOrder():
                prices = self.best_prices(checked.symbol)
                working.marketable = (
                    checked.type is OrderType.LIMIT and prices is not None and prices[1] <= checked.price
                )
                reserve = self._entry_reservation(checked, marketable=working.marketable)
                if reserve > self._balances[self._quote] - self._reserved_quote(excluding=working):
                    return self._reject(working, "insufficient quote balance")
                working.reserved_quote = reserve
            case ExitOrder():
                if self._pending_exit_quantity(checked.symbol) > self._balances[_base(checked.symbol)]:
                    return self._reject(working, "insufficient base balance")
        return self._report(working)

    async def cancel_order(self, symbol: Symbol, client_order_id: str) -> OrderReport:
        if client_order_id.endswith(_OCO_SUFFIX):
            oco = self._oco(client_order_id)
            if oco.status is OrderStatus.NEW:
                oco.status = OrderStatus.CANCELED
            return self._oco_report(oco)
        working = self._working(client_order_id)
        if working.status is OrderStatus.NEW:
            working.status = OrderStatus.CANCELED
            working.reserved_quote = _ZERO
        return self._report(working)

    async def get_order(self, symbol: Symbol, client_order_id: str) -> OrderReport:
        if client_order_id.endswith(_OCO_SUFFIX):
            return self._oco_report(self._oco(client_order_id))
        return self._report(self._working(client_order_id))

    def raise_stop(self, client_order_id: str, new_stop: Decimal) -> OrderReport:
        """Sobe o stop do OCO (trailing/breakeven); nunca desce e fica abaixo do alvo e do bid corrente."""
        oco = self._active_oco(client_order_id)
        if new_stop <= oco.stop:
            raise ValueError("stop can only be raised")
        prices = self.best_prices(oco.symbol)
        if prices is None:
            raise ValueError("no current price to validate the new stop")
        if (oco.target is not None and new_stop >= oco.target) or new_stop >= prices[0]:
            raise ValueError("stop must stay below target and the current bid")
        oco.stop = new_stop
        return self._oco_report(oco)

    def drop_target(self, client_order_id: str) -> OrderReport:
        oco = self._active_oco(client_order_id)
        oco.target = None
        return self._oco_report(oco)

    async def _follow_book(self, symbol: Symbol) -> None:
        async for book in _closing(self._market.stream_order_book(symbol, self._config.book_depth)):
            self.on_book(book)
        raise MarketDataStalledError(f"order book stream ended for {symbol}")

    def _process(self, tick: _Tick, now: datetime) -> None:
        at = tick.at
        self._last_update_at = now
        self._expire(now)
        for working in list(self._orders.values()):
            if self._ready(working, tick) and isinstance(working.order, EntryOrder):
                self._fill_entry(working, working.order, tick, now, at)
        stopped = False
        for oco in list(self._ocos.values()):
            if (
                oco.status is OrderStatus.NEW
                and oco.symbol == tick.symbol
                and oco.armed_at <= tick.window_start
                and tick.low_bid <= oco.stop
            ):
                self._fill_stop(oco, tick, at)
                stopped = True
        for working in list(self._orders.values()):
            if self._ready(working, tick) and isinstance(working.order, ExitOrder):
                if stopped and working.order.type is OrderType.LIMIT:
                    continue
                self._fill_exit(working, working.order, tick, at)
        if stopped:
            return
        for oco in list(self._ocos.values()):
            if (
                oco.status is OrderStatus.NEW
                and oco.symbol == tick.symbol
                and oco.target is not None
                and (
                    oco.armed_at < tick.window_start
                    or (tick.book is None and oco.armed_at == tick.window_start)
                )
                and tick.high_bid >= oco.target
            ):
                self._fill_target(oco, oco.target, at)

    def _ready(self, working: _Working, tick: _Tick) -> bool:
        return (
            working.status is OrderStatus.NEW
            and working.order.symbol == tick.symbol
            and working.executable_at <= tick.window_start
        )

    def _expire(self, now: datetime) -> None:
        ttl = self._config.entry_limit_ttl_seconds
        if ttl is None:
            return
        for working in self._orders.values():
            if (
                working.status is OrderStatus.NEW
                and isinstance(working.order, EntryOrder)
                and working.order.type is OrderType.LIMIT
                and now >= working.submitted_at + timedelta(seconds=ttl)
            ):
                working.status, working.reserved_quote = OrderStatus.EXPIRED, _ZERO

    def _fill_entry(
        self, working: _Working, entry: EntryOrder, tick: _Tick, now: datetime, at: datetime
    ) -> None:
        if entry.type is OrderType.MARKET:
            if tick.book is None:
                return
            base = sweep(tick.book.asks, entry.quantity, allow_shortfall=False)
            if base is None:
                self._reject(working, "insufficient liquidity")
                return
            price = base * (1 + self._config.slippage_bps / _BPS)
            execution = _Execution(
                FillKind.ENTRY_MARKET,
                Liquidity.TAKER,
                self._config.taker_fee,
                price,
                (price - base) * entry.quantity,
            )
        elif working.marketable and tick.book is not None and tick.book.asks[0].price <= entry.price:
            base = sweep(tick.book.asks, entry.quantity, allow_shortfall=False)
            if base is None:
                return
            price = min(base * (1 + self._config.slippage_bps / _BPS), entry.price)
            execution = _Execution(
                FillKind.ENTRY_LIMIT,
                Liquidity.TAKER,
                self._config.taker_fee,
                price,
                max(price - base, _ZERO) * entry.quantity,
            )
        elif working.marketable and tick.book is None and tick.low_ask <= entry.price:
            execution = _Execution(
                FillKind.ENTRY_LIMIT, Liquidity.TAKER, self._config.taker_fee, entry.price, _ZERO
            )
        elif tick.low_ask <= entry.price:
            execution = _Execution(
                FillKind.ENTRY_LIMIT, Liquidity.MAKER, self._config.maker_fee, entry.price, _ZERO
            )
        else:
            return
        others = self._reserved_quote(excluding=working)
        working.reserved_quote = _ZERO
        cost = entry.quantity * execution.price
        fee = cost * execution.fee_rate
        if cost + fee > self._balances[self._quote] - others:
            self._reject(working, "insufficient quote balance at fill")
            return
        self._balances[self._quote] -= cost + fee
        self._balances[_base(entry.symbol)] += entry.quantity
        working.status, working.filled = OrderStatus.FILLED, entry.quantity
        working.average_price = execution.price
        self._record(
            working.order_id, entry.client_order_id, entry.symbol, "buy", entry.quantity, execution, None, at
        )
        self._track_buy(entry.symbol, entry.quantity, execution.price, fee, at)
        self._order_seq += 1
        oco = _Oco(
            entry_id=entry.client_order_id,
            order_id=f"paper-{self._order_seq}",
            symbol=entry.symbol,
            quantity=entry.quantity,
            stop=entry.stop_loss,
            target=entry.take_profit,
            armed_at=self._arm_time(entry, tick),
        )
        self._ocos[oco.client_order_id] = oco

    @staticmethod
    def _arm_time(entry: EntryOrder, tick: _Tick) -> datetime:
        """Book: o proprio tick. Vela: fim da vela do fill (anti-lookahead), salvo minima abaixo do stop."""
        if tick.book is None and tick.low_bid <= entry.stop_loss:
            return tick.window_start
        return tick.at

    def _fill_exit(self, working: _Working, exit_: ExitOrder, tick: _Tick, now: datetime) -> None:
        base_asset = _base(exit_.symbol)
        if exit_.quantity > self._balances[base_asset]:
            self._reject(working, "insufficient base balance at fill")
            return
        if exit_.type is OrderType.MARKET:
            if tick.book is None:
                return
            base = sweep(
                tick.book.bids, exit_.quantity, allow_shortfall=True, shortfall_factor=self._thin_factor()
            )
            if base is None:
                return
            price = base * (1 - self._config.slippage_bps / _BPS)
            execution = _Execution(
                FillKind.EXIT_MARKET,
                Liquidity.TAKER,
                self._config.taker_fee,
                price,
                (base - price) * exit_.quantity,
            )
        elif exit_.price is not None and tick.high_bid >= exit_.price:
            execution = _Execution(
                FillKind.EXIT_LIMIT, Liquidity.MAKER, self._config.maker_fee, exit_.price, _ZERO
            )
        else:
            return
        unprotected = self._balances[base_asset] - self._protected(exit_.symbol)
        self._settle_sell(exit_.symbol, exit_.quantity, execution)
        working.status, working.filled = OrderStatus.FILLED, exit_.quantity
        working.average_price = execution.price
        self._record(
            working.order_id,
            exit_.client_order_id,
            exit_.symbol,
            "sell",
            exit_.quantity,
            execution,
            ExitReason.MANUAL,
            now,
        )
        self._shrink_ocos(exit_.symbol, exit_.quantity - max(unprotected, _ZERO))

    def _fill_stop(self, oco: _Oco, tick: _Tick, now: datetime) -> None:
        if tick.book is not None:
            swept = sweep(
                tick.book.bids, oco.quantity, allow_shortfall=True, shortfall_factor=self._thin_factor()
            )
            base = min(swept if swept is not None else tick.low_bid, oco.stop)
        else:
            base = min(oco.stop, tick.candle_open or oco.stop)
        price = base * (1 - self._config.slippage_bps / _BPS)
        execution = _Execution(
            FillKind.STOP_LOSS,
            Liquidity.TAKER,
            self._config.taker_fee,
            price,
            (base - price) * oco.quantity,
        )
        self._close_oco(oco, execution, ExitReason.STOP_LOSS, now)

    def _fill_target(self, oco: _Oco, target: Decimal, now: datetime) -> None:
        execution = _Execution(FillKind.TAKE_PROFIT, Liquidity.MAKER, self._config.maker_fee, target, _ZERO)
        self._close_oco(oco, execution, ExitReason.TAKE_PROFIT, now)

    def _close_oco(self, oco: _Oco, execution: _Execution, reason: ExitReason, now: datetime) -> None:
        self._settle_sell(oco.symbol, oco.quantity, execution)
        oco.status, oco.filled, oco.average_price = OrderStatus.FILLED, oco.quantity, execution.price
        self._record(
            oco.order_id, oco.client_order_id, oco.symbol, "sell", oco.quantity, execution, reason, now
        )

    def _settle_sell(self, symbol: Symbol, quantity: Decimal, execution: _Execution) -> None:
        gross = quantity * execution.price
        fee = gross * execution.fee_rate
        self._balances[_base(symbol)] -= quantity
        self._balances[self._quote] += gross - fee
        position = self._positions.setdefault(symbol, _Position())
        position.realized_pnl += (execution.price - position.average_price) * quantity
        position.fees_paid += fee
        position.quantity -= quantity
        if position.quantity == 0:
            position.average_price, position.opened_at = _ZERO, None

    def _track_buy(
        self, symbol: Symbol, quantity: Decimal, price: Decimal, fee: Decimal, now: datetime
    ) -> None:
        position = self._positions.setdefault(symbol, _Position())
        total = position.quantity + quantity
        position.average_price = (position.average_price * position.quantity + price * quantity) / total
        position.quantity = total
        position.fees_paid += fee
        position.opened_at = position.opened_at or now

    def _shrink_ocos(self, symbol: Symbol, excess: Decimal) -> None:
        for oco in self._ocos.values():
            if excess <= 0:
                return
            if oco.status is OrderStatus.NEW and oco.symbol == symbol:
                cut = min(oco.quantity, excess)
                oco.quantity -= cut
                excess -= cut
                if oco.quantity == 0:
                    oco.status = OrderStatus.CANCELED

    def _record(
        self,
        order_id: str,
        client_order_id: str,
        symbol: Symbol,
        side: Literal["buy", "sell"],
        quantity: Decimal,
        execution: _Execution,
        reason: ExitReason | None,
        at: datetime,
    ) -> None:
        self._fills.append(
            Fill(
                sequence=self._next_fill_sequence(),
                order_id=order_id,
                client_order_id=client_order_id,
                symbol=symbol,
                side=side,
                kind=execution.kind,
                liquidity=execution.liquidity,
                quantity=quantity,
                price=execution.price,
                fee=quantity * execution.price * execution.fee_rate,
                slippage_cost=execution.slippage,
                exit_reason=reason,
                at=at,
            )
        )

    def _next_fill_sequence(self) -> int:
        self._fill_seq += 1
        return self._fill_seq

    def _thin_factor(self) -> Decimal:
        return 1 - self._config.thin_book_slippage_bps / _BPS

    def _entry_reservation(self, entry: EntryOrder, *, marketable: bool) -> Decimal:
        if entry.type is OrderType.LIMIT:
            fee = (
                max(self._config.maker_fee, self._config.taker_fee) if marketable else self._config.maker_fee
            )
            return entry.quantity * entry.price * (1 + fee)
        prices = self.best_prices(entry.symbol)
        reference = max(entry.price, prices[1]) if prices else entry.price
        slipped = reference * (1 + self._config.slippage_bps / _BPS)
        return entry.quantity * slipped * (1 + self._config.taker_fee)

    def _pending(self) -> list[_Working]:
        return [w for w in self._orders.values() if w.status is OrderStatus.NEW]

    def _reserved_quote(self, excluding: _Working | None = None) -> Decimal:
        return sum((w.reserved_quote for w in self._pending() if w is not excluding), _ZERO)

    def _pending_exit_quantity(self, symbol: Symbol) -> Decimal:
        return sum(
            (
                w.order.quantity
                for w in self._pending()
                if isinstance(w.order, ExitOrder) and w.order.symbol == symbol
            ),
            _ZERO,
        )

    def _protected(self, symbol: Symbol) -> Decimal:
        return sum(
            (o.quantity for o in self._ocos.values() if o.status is OrderStatus.NEW and o.symbol == symbol),
            _ZERO,
        )

    def _fresh_book(self, symbol: Symbol) -> OrderBookSnapshot | None:
        entry = self._books.get(symbol)
        if entry is None:
            return None
        book, seen_at = entry
        age = self._clock.now() - seen_at
        return book if age <= timedelta(seconds=self._config.max_book_age_seconds) else None

    def _reject(self, working: _Working, reason: str) -> OrderReport:
        working.status, working.rejection, working.reserved_quote = OrderStatus.REJECTED, reason, _ZERO
        return self._report(working)

    def _report(self, working: _Working) -> OrderReport:
        return OrderReport(
            order_id=working.order_id,
            client_order_id=working.order.client_order_id,
            symbol=working.order.symbol,
            status=working.status,
            filled_quantity=working.filled,
            average_price=working.average_price,
        )

    def _oco_report(self, oco: _Oco) -> OrderReport:
        return OrderReport(
            order_id=oco.order_id,
            client_order_id=oco.client_order_id,
            symbol=oco.symbol,
            status=oco.status,
            filled_quantity=oco.filled,
            average_price=oco.average_price,
        )

    def _working(self, client_order_id: str) -> _Working:
        try:
            return self._orders[client_order_id]
        except KeyError:
            raise UnknownOrderError(client_order_id) from None

    def _oco(self, client_order_id: str) -> _Oco:
        try:
            return self._ocos[client_order_id]
        except KeyError:
            raise UnknownOrderError(client_order_id) from None

    def _active_oco(self, entry_client_order_id: str) -> _Oco:
        oco = self._ocos.get(f"{entry_client_order_id}{_OCO_SUFFIX}")
        if oco is None or oco.status is not OrderStatus.NEW:
            raise UnknownOrderError(entry_client_order_id)
        return oco


def _base(symbol: Symbol) -> str:
    return symbol.split("/")[0]


def _position_state(symbol: Symbol, position: _Position) -> PaperPosition:
    return PaperPosition(
        symbol=symbol,
        quantity=position.quantity,
        average_price=position.average_price,
        opened_at=position.opened_at,
        realized_pnl=position.realized_pnl,
        fees_paid=position.fees_paid,
    )


async def _closing[T](stream: AsyncIterator[T]) -> AsyncIterator[T]:
    try:
        async for item in stream:
            yield item
    finally:
        if (close := getattr(stream, "aclose", None)) is not None:
            await close()
