"""Dublês do runner: diario em memoria (com falha injetavel), barramento gravador, mercado e relogio fake."""

import contextlib
import copy
from collections.abc import AsyncIterator, Callable, Iterable, Iterator, Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from engine.application.backtest.models import EquityPoint
from engine.application.risk import EventCalendar, ManagedPosition, RiskState
from engine.application.risk.sizing import SymbolRules
from engine.application.strategies import Strategy
from engine.domain.candle import Candle
from engine.domain.journal import ControlEvent, DecisionEntry, FillRecord, OrderRecord, RunRecord
from engine.domain.market import BookLevel, OrderBookSnapshot
from engine.domain.risk import RiskConfig
from engine.domain.trade import Trade
from engine.domain.types import Symbol, Timeframe
from engine.infrastructure.persistence.decision_repository import DecisionConflictError
from engine.infrastructure.persistence.order_repository import FillConflictError, next_order_state
from engine.infrastructure.persistence.position_repository import check_position_transition
from engine.infrastructure.persistence.recovery import RecoveryState
from engine.infrastructure.persistence.serialization import ConflictError, PersistenceError
from engine.infrastructure.persistence.state_repository import GatewaySnapshot
from engine.infrastructure.persistence.trade_repository import TradeConflictError
from engine.runner.paper_config import PaperRunConfig, RunPaths
from engine.runner.paper_journal import Journal
from engine.runner.paper_robot import PaperRobot
from tests.unit.backtest.engine_helpers import T0, Scripted, mk

D = Decimal
SYMBOL = "BTC/USDT"


class SimulatedCrash(RuntimeError):
    """Queda injetada: a operacao NAO chega ao diario (como uma transacao revertida)."""


class FakeClock:
    def __init__(self, start: datetime = T0) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def set(self, moment: datetime) -> None:
        self._now = moment

    def advance(self, seconds: float = 0, ms: int = 0) -> None:
        self._now += timedelta(seconds=seconds, milliseconds=ms)


class MemoryJournal:
    """Mesmo contrato E MESMAS REGRAS do `SqlJournal`, sem banco.

    A maquina de estados das ordens e a do SQL (`next_order_state`: ranking de status, campos fixos, terminal
    imutavel, `OrderConflictError`), fills/decisoes/trades sao unicos por run e idempotentes (conteudo
    diferente com a mesma chave e conflito) e toda gravacao e atomica (falha desfaz tudo), como a transacao.
    `crash_on` lista metodos que falham antes de gravar; `inherited_halt` simula o HALT de um run anterior.
    """

    def __init__(self, run_id: str = "test-run") -> None:
        self.run_id = run_id
        self.run: RunRecord | None = None
        self.config: dict[str, Any] = {}
        self.decisions: list[DecisionEntry] = []
        self.orders: dict[str, OrderRecord] = {}
        self.fills: dict[str, FillRecord] = {}
        self.trades: list[Trade] = []
        self.positions: dict[str, ManagedPosition] = {}
        self.risk_states: list[RiskState] = []
        self.gateway_states: list[dict[str, Any]] = []
        self.equity: list[EquityPoint] = []
        self.controls: list[ControlEvent] = []
        self.audits: list[Mapping[str, Any]] = []
        self.crash_on: set[str] = set()
        self.calls: list[str] = []
        self.inherited_halt: tuple[str, str] | None = None

    def _enter(self, name: str) -> None:
        self.calls.append(name)
        if name in self.crash_on:
            raise SimulatedCrash(name)

    @contextlib.contextmanager
    def _transaction(self) -> Iterator[None]:
        saved = (
            dict(self.orders),
            dict(self.fills),
            list(self.trades),
            dict(self.positions),
            list(self.risk_states),
            list(self.gateway_states),
            list(self.decisions),
            list(self.controls),
            list(self.audits),
        )
        try:
            yield
        except BaseException:
            (
                self.orders,
                self.fills,
                self.trades,
                self.positions,
                self.risk_states,
                self.gateway_states,
                self.decisions,
                self.controls,
                self.audits,
            ) = saved  # type: ignore[assignment]
            raise

    def _states(self, risk: RiskState | None, gateway: Mapping[str, Any] | None) -> None:
        if risk is not None:
            self.risk_states.append(risk)
        if gateway is not None:
            self.gateway_states.append(copy.deepcopy(dict(gateway)))

    def _upsert_order(self, order: OrderRecord) -> None:
        stored = self.orders.get(order.client_order_id)
        if stored is None:
            self.orders[order.client_order_id] = order
            return
        if (state := next_order_state(stored, order)) is None:
            return
        self.orders[order.client_order_id] = stored.model_copy(
            update={
                "status": state.status,
                "exchange_order_id": state.exchange_order_id or stored.exchange_order_id,
                "filled_quantity": state.filled_quantity,
                "average_price": state.average_price or stored.average_price,
                "updated_at": state.updated_at,
            }
        )

    def _record_fill(self, fill: FillRecord) -> bool:
        stored = self.fills.get(fill.fill_id)
        if stored is None:
            self.fills[fill.fill_id] = fill
            return True
        if stored != fill:
            raise FillConflictError(f"fill {fill.fill_id} ja registrado com conteudo diferente")
        return False

    def _record_decision(self, entry: DecisionEntry) -> bool:
        key = entry.decision_key(self.run_id)
        for stored in self.decisions:
            if stored.decision_key(self.run_id) == key:
                if stored != entry:
                    raise DecisionConflictError(f"decisao {key} ja registrada com conteudo diferente")
                return False
        self.decisions.append(entry)
        return True

    def _record_trade(self, trade: Trade) -> bool:
        for stored in self.trades:
            if stored.trade_id == trade.trade_id:
                if stored != trade:
                    raise TradeConflictError(f"trade {trade.trade_id} ja registrado com conteudo diferente")
                return False
        self.trades.append(trade)
        return True

    def _save_position(self, position: ManagedPosition) -> None:
        stored = self.positions.get(position.symbol)
        if stored is not None:
            check_position_transition(stored, position)
        self.positions[position.symbol] = position

    def _control(self, event: ControlEvent, audit: Mapping[str, Any] | None = None) -> None:
        self.controls.append(event)
        self.audits.append({"action": event.action.value, **(audit or {})})

    async def load(self) -> RecoveryState:
        gateway = (
            None
            if not self.gateway_states
            else GatewaySnapshot(len(self.gateway_states), T0, copy.deepcopy(self.gateway_states[-1]))
        )
        return RecoveryState(
            run=self.run,
            risk_state=self.risk_states[-1] if self.risk_states else None,
            gateway_state=gateway,
            open_positions=tuple(self.positions.values()),
            open_orders=tuple(o for o in self.orders.values() if o.is_open),
        )

    async def previous_halt(self) -> tuple[str, str] | None:
        return self.inherited_halt

    async def start_run(self, run: RunRecord, config: Mapping[str, Any], event: ControlEvent) -> None:
        self._enter("start_run")
        self.run = self.run or run
        self.config = dict(config)
        self.controls.append(event)

    async def record_decision(
        self,
        entry: DecisionEntry,
        *,
        order: OrderRecord | None = None,
        gateway_state: Mapping[str, Any] | None = None,
    ) -> bool:
        self._enter("record_decision")
        with self._transaction():
            try:
                created = self._record_decision(entry)
            except ConflictError:
                created = False
            if order is not None:
                self._upsert_order(order)
            self._states(None, gateway_state)
        return created

    async def has_decision(self, symbol: str, decided_at: datetime) -> bool:
        return any(d.symbol == symbol and d.decided_at == decided_at for d in self.decisions)

    async def update_order(
        self, order: OrderRecord, *, gateway_state: Mapping[str, Any] | None = None
    ) -> None:
        self._enter("update_order")
        with self._transaction():
            self._upsert_order(order)
            self._states(None, gateway_state)

    async def record_entry(
        self,
        *,
        orders: Sequence[OrderRecord],
        fill: FillRecord,
        position: ManagedPosition | None,
        risk_state: RiskState,
        gateway_state: Mapping[str, Any],
        decision: DecisionEntry | None = None,
    ) -> bool:
        self._enter("record_entry")
        with self._transaction():
            if decision is not None:
                with contextlib.suppress(ConflictError):
                    self._record_decision(decision)
            entry, *others = orders
            self._upsert_order(entry)
            applied = self._record_fill(fill)
            if applied:
                if position is not None:
                    self._save_position(position)
                self._states(risk_state, gateway_state)
                for order in others:
                    self._upsert_order(order)
            return applied

    async def record_exit(
        self,
        *,
        orders: Sequence[OrderRecord],
        fill: FillRecord,
        trade: Trade,
        position: ManagedPosition | None,
        risk_state: RiskState,
        strategy: str | None,
        risk_amount: Any,
        gateway_state: Mapping[str, Any],
        without_position_row: bool = False,
        halt: ControlEvent | None = None,
    ) -> None:
        self._enter("record_exit")
        with self._transaction():
            for order in orders:
                self._upsert_order(order)
            recorded = self._record_trade(trade)
            if fill.client_order_id not in self.orders:
                raise PersistenceError(f"ordem {fill.client_order_id} do fill {fill.fill_id} nao registrada")
            self._record_fill(fill)
            if not recorded:
                return
            if not without_position_row:
                if position is None:
                    if self.positions.pop(trade.symbol, None) is None:
                        raise PersistenceError(f"{trade.symbol}: nao ha posicao aberta para encerrar")
                else:
                    self._save_position(position)
            self._states(risk_state, gateway_state)
            if halt is not None:
                self._control(halt)

    async def record_position(self, position: ManagedPosition, gateway_state: Mapping[str, Any]) -> None:
        self._enter("record_position")
        with self._transaction():
            self._save_position(position)
            self._states(None, gateway_state)

    async def save_state(self, risk_state: RiskState, gateway_state: Mapping[str, Any], at: datetime) -> None:
        self._enter("save_state")
        self._states(risk_state, gateway_state)

    async def record_control(
        self,
        event: ControlEvent,
        *,
        risk_state: RiskState | None = None,
        gateway_state: Mapping[str, Any] | None = None,
        audit: Mapping[str, Any] | None = None,
    ) -> None:
        self._enter("record_control")
        with self._transaction():
            self._states(risk_state, gateway_state)
            self._control(event, audit)

    async def record_equity(self, point: EquityPoint) -> bool:
        self._enter("record_equity")
        if any(p.at == point.at for p in self.equity):
            return False
        self.equity.append(point)
        return True

    async def open_positions(self) -> list[ManagedPosition]:
        return list(self.positions.values())

    def codes(self) -> list[str]:
        return [d.code for d in self.decisions]


class RecordingBus:
    healthy = True

    def __init__(self) -> None:
        self.events: list[tuple[str, Mapping[str, Any]]] = []

    async def publish(self, kind: str, payload: Mapping[str, Any]) -> None:
        self.events.append((kind, payload))

    async def close(self) -> None:
        return None

    def of(self, kind: str) -> list[Mapping[str, Any]]:
        return [p for k, p in self.events if k == kind]


class FakeFeed:
    """Mercado fake: historico para o bootstrap/recuperacao e fluxos pre-carregados."""

    def __init__(
        self,
        candles: Iterable[Candle] = (),
        books: Iterable[OrderBookSnapshot] = (),
        stream: Iterable[Candle] = (),
    ) -> None:
        self.candles = list(candles)
        self.books = list(books)
        self.stream = list(stream)
        self.fetch_calls: list[tuple[datetime, datetime | None]] = []
        self.fail_fetch = False

    async def fetch_candles(
        self, symbol: Symbol, timeframe: Timeframe, start: datetime, end: datetime | None = None
    ) -> list[Candle]:
        self.fetch_calls.append((start, end))
        if self.fail_fetch:
            raise ConnectionError("rest indisponivel")
        return [c for c in self.candles if c.symbol == symbol and c.open_time >= start]

    async def stream_candles(
        self, symbol: Symbol, timeframe: Timeframe, since: datetime | None = None
    ) -> AsyncIterator[Candle]:
        for candle in self.stream:
            if candle.symbol == symbol and (since is None or candle.open_time >= since):
                yield candle

    async def stream_order_book(self, symbol: Symbol, depth: int = 10) -> AsyncIterator[OrderBookSnapshot]:
        for item in self.books:
            if item.symbol == symbol:
                yield item


def make_book(bid: str, ask: str, symbol: str = SYMBOL) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        symbol=symbol,
        bids=(BookLevel(price=D(bid), quantity=D(100)),),
        asks=(BookLevel(price=D(ask), quantity=D(100)),),
        received_at=T0,
    )


def candle_at(i: int, o: str, h: str, low: str, c: str, volume: str = "1000") -> Candle:
    return mk(i, o, h, low, c, volume)


def warmup(count: int = 29) -> list[Candle]:
    return [mk(i, "100", "100.2", "99.8", "100") for i in range(count)]


def config(**overrides: Any) -> PaperRunConfig:
    values: dict[str, Any] = {
        "symbols": (SYMBOL,),
        "slippage_bps": D(0),
        "latency_ms": 0,
        "allow_open_calendar": True,
        "history_window": 60,
        "risk": RiskConfig(),
    }
    return PaperRunConfig.model_validate(values | overrides)


def rules() -> dict[str, SymbolRules]:
    return {
        SYMBOL: SymbolRules(
            symbol=SYMBOL,
            tick_size=D("0.01"),
            step_size=D("0.00001"),
            min_quantity=D("0.00001"),
            min_notional=D(5),
        ),
        "ETH/USDT": SymbolRules(
            symbol="ETH/USDT",
            tick_size=D("0.01"),
            step_size=D("0.0001"),
            min_quantity=D("0.0001"),
            min_notional=D(5),
        ),
    }


class Harness:
    """Robo com tudo fake. Relogio e velas avancam juntos (latencia de execucao = 0, como no backtest)."""

    def __init__(
        self,
        strategy_indexes: set[int] | None = None,
        *,
        journal: Journal | None = None,
        history: Sequence[Candle] | None = None,
        cfg: PaperRunConfig | None = None,
        calendar: EventCalendar | None = None,
        clock: FakeClock | None = None,
        feed: FakeFeed | None = None,
        quotes: Callable[[str], tuple[Decimal, Decimal] | None] | None = None,
        paths: RunPaths | None = None,
        strategies: Sequence[Strategy] | None = None,
    ) -> None:
        self.clock = clock or FakeClock(T0 + timedelta(hours=29))
        self.journal: Any = journal or MemoryJournal()
        self.bus = RecordingBus()
        self.history = list(warmup() if history is None else history)
        self.feed = feed or FakeFeed(self.history)
        self.cfg = cfg or config()
        self.strategy = Scripted({29} if strategy_indexes is None else strategy_indexes)
        self.robot = PaperRobot(
            self.cfg,
            run_id=self.journal.run_id,
            journal=self.journal,
            bus=self.bus,
            market=self.feed,
            strategies=list(strategies) if strategies else [self.strategy],
            rules=rules(),
            calendar=calendar or EventCalendar(),
            clock=self.clock,
            quotes=quotes,
            paths=paths,
            persist_retry_delays=(0.001, 0.001, 0.001),
        )

    async def start(self) -> "Harness":
        await self.robot.start()
        return self

    async def tick(self, bid: str = "99.99", ask: str = "100.01") -> None:
        await self.robot.on_book(make_book(bid, ask))

    async def close(self, candle: Candle, bid: str | None = None, ask: str | None = None) -> None:
        """Fecha a vela: relogio no `close_time`, book em torno do fechamento, depois a vela."""
        self.clock.set(candle.close_time)
        mid = candle.close
        await self.tick(bid or str(mid - D("0.01")), ask or str(mid + D("0.01")))
        await self.robot.on_candle(candle)
