import itertools
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal

from engine.domain.candle import Candle
from engine.domain.gateway import DuplicateOrderError, UnknownOrderError
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
from engine.infrastructure.backtest.config import BacktestConfig
from engine.infrastructure.backtest.fills import Fill, FillKind, Liquidity

_BPS = Decimal(10_000)
_OCO_SUFFIX = ":oco"


@dataclass
class _Working:
    order: EntryOrder | ExitOrder
    order_id: str
    first_index: int
    status: OrderStatus = OrderStatus.NEW
    filled: Decimal = Decimal(0)
    average_price: Decimal | None = None
    reserved_quote: Decimal = Decimal(0)
    rejection: str | None = None


@dataclass
class _Oco:
    entry_id: str
    order_id: str
    symbol: Symbol
    quantity: Decimal
    stop: Decimal
    target: Decimal | None
    entry_index: int
    status: OrderStatus = OrderStatus.NEW
    average_price: Decimal | None = None
    filled: Decimal = Decimal(0)

    @property
    def client_order_id(self) -> str:
        return f"{self.entry_id}{_OCO_SUFFIX}"


@dataclass
class _Book:
    orders: dict[str, _Working] = field(default_factory=dict)
    ocos: dict[str, _Oco] = field(default_factory=dict)


@dataclass(frozen=True)
class _Execution:
    kind: FillKind
    liquidity: Liquidity
    fee_rate: Decimal
    price: Decimal
    slippage: Decimal
    at: datetime


class BacktestGateway:
    """ExchangeGateway sobre velas historicas fechadas, com relogio simulado e execucao pessimista.

    O estado so muda em `advance()`; ordens enviadas na vela i executam a partir da vela i + latencia.
    Pessimismo: limit executa no preco limite; stop e alvo na mesma vela => stop (inclusive um limit de venda
    manual na mesma vela em que o stop do OCO foi tocado); na vela de entrada so o stop e avaliado; gap
    abaixo do stop executa na abertura; slippage sempre contra o trader; taxas debitadas em moeda de cotacao.
    Fills de abertura levam `open_time`; os intra-vela, `close_time`.
    """

    def __init__(self, candles: list[Candle], config: BacktestConfig | None = None) -> None:
        self._candles = _validated(candles)
        self._config = config or BacktestConfig()
        first = self._candles[0]
        self.symbol: Symbol = first.symbol
        self.timeframe: Timeframe = first.timeframe
        self._base, self._quote = first.symbol.split("/")
        self._cursor = -1
        self._quote_total = self._config.initial_quote
        self._base_total = Decimal(0)
        self._book = _Book()
        self._fills: list[Fill] = []
        self._order_seq = 0

    @property
    def now(self) -> datetime | None:
        return None if self._cursor < 0 else self._candles[self._cursor].close_time

    @property
    def has_next(self) -> bool:
        return self._cursor + 1 < len(self._candles)

    @property
    def fills(self) -> tuple[Fill, ...]:
        return tuple(self._fills)

    @property
    def submitted_orders(self) -> list[Order]:
        return [w.order for w in self._book.orders.values()]

    def rejection_reason(self, client_order_id: str) -> str | None:
        return self._working(client_order_id).rejection

    def advance(self) -> Candle | None:
        if not self.has_next:
            return None
        self._cursor += 1
        candle = self._candles[self._cursor]
        for working in list(self._book.orders.values()):
            if working.status is OrderStatus.NEW and working.first_index <= self._cursor:
                if self._shadowed_by_stop(working, candle):
                    continue
                self._execute(working, candle)
        for oco in list(self._book.ocos.values()):
            if oco.status is OrderStatus.NEW:
                self._execute_oco(oco, candle)
        return candle

    async def fetch_candles(
        self, symbol: Symbol, timeframe: Timeframe, start: datetime, end: datetime | None = None
    ) -> list[Candle]:
        self._check_market(symbol, timeframe)
        return [c for c in self._revealed() if c.open_time >= start and (end is None or c.open_time <= end)]

    async def stream_candles(
        self, symbol: Symbol, timeframe: Timeframe, since: datetime | None = None
    ) -> AsyncIterator[Candle]:
        """Reproduz as velas ja reveladas (desde `since`) e passa a conduzir o relogio vela a vela."""
        self._check_market(symbol, timeframe)
        for candle in self._revealed():
            if since is None or candle.open_time >= since:
                yield candle
        while (new := self.advance()) is not None:
            yield new

    def stream_order_book(self, symbol: Symbol, depth: int = 10) -> AsyncIterator[OrderBookSnapshot]:
        raise NotImplementedError("backtest has no order book")

    async def get_balances(self) -> dict[str, Balance]:
        quote_locked = self._reserved_quote()
        base_locked = min(self._base_total, self._protected() + self._pending_exit_quantity())
        return {
            self._quote: Balance(
                asset=self._quote, free=self._quote_total - quote_locked, locked=quote_locked
            ),
            self._base: Balance(asset=self._base, free=self._base_total - base_locked, locked=base_locked),
        }

    async def place_order(self, order: Order) -> OrderReport:
        checked = revalidate_order(order)
        if checked.client_order_id in self._book.orders:
            raise DuplicateOrderError(checked.client_order_id)
        self._order_seq += 1
        working = _Working(
            order=checked,
            order_id=f"bt-{self._order_seq}",
            first_index=self._cursor + self._config.latency_candles,
        )
        self._book.orders[checked.client_order_id] = working
        if checked.symbol != self.symbol:
            return self._reject(working, "unknown symbol")
        match checked:
            case EntryOrder():
                reserve = self._entry_reservation(checked)
                if reserve > self._quote_total - self._reserved_quote():
                    return self._reject(working, "insufficient quote balance")
                working.reserved_quote = reserve
            case ExitOrder():
                if self._pending_exit_quantity() > self._base_total:
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
            working.reserved_quote = Decimal(0)
        return self._report(working)

    async def get_order(self, symbol: Symbol, client_order_id: str) -> OrderReport:
        if client_order_id.endswith(_OCO_SUFFIX):
            return self._oco_report(self._oco(client_order_id))
        return self._report(self._working(client_order_id))

    def raise_stop(self, client_order_id: str, new_stop: Decimal) -> OrderReport:
        """Sobe o stop do OCO da entrada (trailing/breakeven); nunca desce nem alcanca o ultimo fechamento."""
        oco = self._book.ocos.get(f"{client_order_id}{_OCO_SUFFIX}")
        if oco is None or oco.status is not OrderStatus.NEW:
            raise UnknownOrderError(client_order_id)
        if new_stop <= oco.stop:
            raise ValueError("stop can only be raised")
        if (oco.target is not None and new_stop >= oco.target) or new_stop >= self._candles[
            self._cursor
        ].close:
            raise ValueError("stop must stay below target and the last close")
        oco.stop = new_stop
        return self._oco_report(oco)

    def drop_target(self, client_order_id: str) -> OrderReport:
        """Transforma o OCO da entrada em stop puro: o alvo passa a ser gerido por ordens manuais."""
        oco = self._book.ocos.get(f"{client_order_id}{_OCO_SUFFIX}")
        if oco is None or oco.status is not OrderStatus.NEW:
            raise UnknownOrderError(client_order_id)
        oco.target = None
        return self._oco_report(oco)

    def _shadowed_by_stop(self, working: _Working, candle: Candle) -> bool:
        order = working.order
        if not isinstance(order, ExitOrder) or order.type is not OrderType.LIMIT:
            return False
        return any(o.status is OrderStatus.NEW and candle.low <= o.stop for o in self._book.ocos.values())

    def _execute(self, working: _Working, candle: Candle) -> None:
        match working.order:
            case EntryOrder() as entry:
                self._execute_entry(working, entry, candle)
            case ExitOrder() as exit_:
                self._execute_exit(working, exit_, candle)

    def _execute_entry(self, working: _Working, entry: EntryOrder, candle: Candle) -> None:
        if entry.type is OrderType.MARKET:
            price = candle.open * (1 + self._config.slippage_bps / _BPS)
            execution = _Execution(
                FillKind.ENTRY_MARKET,
                Liquidity.TAKER,
                self._config.taker_fee,
                price,
                (price - candle.open) * entry.quantity,
                candle.open_time,
            )
        elif candle.low <= entry.price:
            execution = _Execution(
                FillKind.ENTRY_LIMIT,
                Liquidity.MAKER,
                self._config.maker_fee,
                entry.price,
                Decimal(0),
                candle.close_time,
            )
        else:
            return
        others = self._reserved_quote() - working.reserved_quote
        working.reserved_quote = Decimal(0)
        cost = entry.quantity * execution.price
        fee = cost * execution.fee_rate
        if cost + fee > self._quote_total - others:
            working.status, working.rejection = OrderStatus.REJECTED, "insufficient quote balance at fill"
            return
        self._quote_total -= cost + fee
        self._base_total += entry.quantity
        working.status, working.filled = OrderStatus.FILLED, entry.quantity
        working.average_price = execution.price
        self._record(working.order_id, entry.client_order_id, entry, "buy", entry.quantity, execution, None)
        self._order_seq += 1
        oco = _Oco(
            entry_id=entry.client_order_id,
            order_id=f"bt-{self._order_seq}",
            symbol=entry.symbol,
            quantity=entry.quantity,
            stop=entry.stop_loss,
            target=entry.take_profit,
            entry_index=self._cursor,
        )
        self._book.ocos[oco.client_order_id] = oco

    def _execute_exit(self, working: _Working, exit_: ExitOrder, candle: Candle) -> None:
        if exit_.quantity > self._base_total:
            working.status, working.rejection = OrderStatus.REJECTED, "insufficient base balance at fill"
            return
        if exit_.type is OrderType.MARKET:
            price = candle.open * (1 - self._config.slippage_bps / _BPS)
            execution = _Execution(
                FillKind.EXIT_MARKET,
                Liquidity.TAKER,
                self._config.taker_fee,
                price,
                (candle.open - price) * exit_.quantity,
                candle.open_time,
            )
        elif exit_.price is not None and candle.high >= exit_.price:
            execution = _Execution(
                FillKind.EXIT_LIMIT,
                Liquidity.MAKER,
                self._config.maker_fee,
                exit_.price,
                Decimal(0),
                candle.close_time,
            )
        else:
            return
        unprotected = self._base_total - self._protected()
        self._settle_sell(exit_.quantity, execution)
        working.status, working.filled = OrderStatus.FILLED, exit_.quantity
        working.average_price = execution.price
        self._record(
            working.order_id,
            exit_.client_order_id,
            exit_,
            "sell",
            exit_.quantity,
            execution,
            ExitReason.MANUAL,
        )
        self._shrink_ocos(exit_.quantity - max(unprotected, Decimal(0)))

    def _execute_oco(self, oco: _Oco, candle: Candle) -> None:
        if candle.low <= oco.stop:
            base = min(oco.stop, candle.open)
            price = base * (1 - self._config.slippage_bps / _BPS)
            execution = _Execution(
                FillKind.STOP_LOSS,
                Liquidity.TAKER,
                self._config.taker_fee,
                price,
                (base - price) * oco.quantity,
                candle.open_time if candle.open <= oco.stop else candle.close_time,
            )
            reason = ExitReason.STOP_LOSS
        elif oco.target is not None and candle.high >= oco.target and oco.entry_index < self._cursor:
            execution = _Execution(
                FillKind.TAKE_PROFIT,
                Liquidity.MAKER,
                self._config.maker_fee,
                oco.target,
                Decimal(0),
                candle.close_time,
            )
            reason = ExitReason.TAKE_PROFIT
        else:
            return
        self._settle_sell(oco.quantity, execution)
        oco.status, oco.filled, oco.average_price = OrderStatus.FILLED, oco.quantity, execution.price
        self._record(oco.order_id, oco.client_order_id, oco, "sell", oco.quantity, execution, reason)

    def _settle_sell(self, quantity: Decimal, execution: _Execution) -> None:
        self._base_total -= quantity
        self._quote_total += quantity * execution.price * (1 - execution.fee_rate)

    def _shrink_ocos(self, excess: Decimal) -> None:
        for oco in self._book.ocos.values():
            if excess <= 0:
                return
            if oco.status is OrderStatus.NEW:
                cut = min(oco.quantity, excess)
                oco.quantity -= cut
                excess -= cut
                if oco.quantity == 0:
                    oco.status = OrderStatus.CANCELED

    def _record(
        self,
        order_id: str,
        client_order_id: str,
        source: EntryOrder | ExitOrder | _Oco,
        side: Literal["buy", "sell"],
        quantity: Decimal,
        execution: _Execution,
        reason: ExitReason | None,
    ) -> None:
        self._fills.append(
            Fill(
                sequence=len(self._fills) + 1,
                order_id=order_id,
                client_order_id=client_order_id,
                symbol=source.symbol,
                side=side,
                kind=execution.kind,
                liquidity=execution.liquidity,
                quantity=quantity,
                price=execution.price,
                fee=quantity * execution.price * execution.fee_rate,
                slippage_cost=execution.slippage,
                exit_reason=reason,
                at=execution.at,
            )
        )

    def _entry_reservation(self, entry: EntryOrder) -> Decimal:
        if entry.type is OrderType.LIMIT:
            return entry.quantity * entry.price * (1 + self._config.maker_fee)
        slipped = entry.price * (1 + self._config.slippage_bps / _BPS)
        return entry.quantity * slipped * (1 + self._config.taker_fee)

    def _pending(self) -> list[_Working]:
        return [w for w in self._book.orders.values() if w.status is OrderStatus.NEW]

    def _reserved_quote(self) -> Decimal:
        return sum((w.reserved_quote for w in self._pending()), Decimal(0))

    def _pending_exit_quantity(self) -> Decimal:
        return sum((w.order.quantity for w in self._pending() if isinstance(w.order, ExitOrder)), Decimal(0))

    def _protected(self) -> Decimal:
        return sum((o.quantity for o in self._book.ocos.values() if o.status is OrderStatus.NEW), Decimal(0))

    def _reject(self, working: _Working, reason: str) -> OrderReport:
        working.status, working.rejection = OrderStatus.REJECTED, reason
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
            return self._book.orders[client_order_id]
        except KeyError:
            raise UnknownOrderError(client_order_id) from None

    def _oco(self, client_order_id: str) -> _Oco:
        try:
            return self._book.ocos[client_order_id]
        except KeyError:
            raise UnknownOrderError(client_order_id) from None

    def _revealed(self) -> list[Candle]:
        return self._candles[: self._cursor + 1]

    def _check_market(self, symbol: Symbol, timeframe: Timeframe) -> None:
        if (symbol, timeframe) != (self.symbol, self.timeframe):
            raise ValueError(f"backtest holds {self.symbol} {self.timeframe}, not {symbol} {timeframe}")


def _validated(candles: list[Candle]) -> list[Candle]:
    if not candles:
        raise ValueError("candles must not be empty")
    first = candles[0]
    step = timedelta(seconds=first.timeframe.seconds)
    for previous, current in itertools.pairwise(candles):
        if current.open_time - previous.open_time != step:
            raise ValueError("candles must be ordered and contiguous")
    if any(not c.closed or (c.symbol, c.timeframe) != (first.symbol, first.timeframe) for c in candles):
        raise ValueError("candles must be closed and share symbol/timeframe")
    return list(candles)
