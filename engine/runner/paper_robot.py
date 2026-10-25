"""Robo de paper trading: o mesmo pipeline de estrategia e risco do backtest, vela a vela, ao vivo.

Concorrencia: TODA mutacao do gateway (book, vela, ordens) e todo processamento de fills ocorrem sob `_lock`,
de modo que o estado do gateway gravado junto de cada evento do diario e sempre coerente com o que ja foi
persistido. Metodos com prefixo `_` assumem o lock tomado; os publicos o tomam.
"""

import asyncio
import contextlib
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from functools import partial
from pathlib import Path
from typing import Any

import structlog
from pydantic import ValidationError

from engine.application.ai import AI_LOW_CONFIDENCE, AnalystOpinion, MarketAnalyst, build_request, combine
from engine.application.ai.lessons import LessonSource, lesson_notes
from engine.application.ai.models import MAX_LESSONS
from engine.application.backtest.models import EquityPoint
from engine.application.regime import classify_regime
from engine.application.risk import (
    AccountSnapshot,
    ApprovedEntry,
    AuditAction,
    AuditEntry,
    Divergence,
    DivergenceKind,
    EmergencyExit,
    EventCalendar,
    ExitFill,
    ManagedPosition,
    MarketSnapshot,
    ProtectiveStop,
    Rejection,
    RiskState,
    SymbolRules,
    close_position,
    emergency_trade,
    evaluate_entry,
    exit_fill_id,
    expire_pending,
    manage_position,
    open_position,
    plan_partial,
    reconcile,
    rolling_quote_volume,
)
from engine.application.risk.filters import RunMode, history_problem
from engine.application.selector import (
    SELECTOR_IDLE,
    SELECTOR_NO_SCORES,
    ScoreKey,
    StrategyScore,
    TradeSample,
    build_scores,
    select_for_reading,
    selection_insight,
)
from engine.application.strategies import Strategy, StrategyContext, StrategySignal
from engine.application.trading_config import Effect, risk_effect
from engine.domain.candle import Candle
from engine.domain.decision import DecisionAction
from engine.domain.gateway import DuplicateOrderError, MarketDataGateway, UnknownOrderError
from engine.domain.journal import (
    ControlAction,
    ControlEvent,
    DecisionEntry,
    FillRecord,
    OrderRecord,
    RunRecord,
)
from engine.domain.journal import (
    RunMode as JournalRunMode,
)
from engine.domain.market import OrderBookSnapshot
from engine.domain.order import EntryOrder, ExitOrder, OrderReport, OrderStatus, OrderType
from engine.domain.risk import RiskConfig
from engine.domain.signal import Signal
from engine.domain.trade import ExitReason, Trade
from engine.infrastructure.backtest import Fill
from engine.infrastructure.config.logging import scrub_text
from engine.infrastructure.notifications import AlertDispatcher
from engine.infrastructure.paper import Clock, PaperGateway, PaperHealth
from engine.runner.command_channel import AuditSink, DiscardLimiter, Outcome, SignedCommandSource
from engine.runner.paper_config import PaperRunConfig, RunPaths
from engine.runner.paper_control import (
    Command,
    CommandName,
    CommandQueue,
    CommandResult,
    CommandStatus,
    write_status,
)
from engine.runner.paper_events import EventBus
from engine.runner.paper_journal import Journal
from engine.runner.paper_outbox import (
    DEFAULT_RETRY_DELAYS,
    DurableJournal,
    Outbox,
    PersistenceUnavailableError,
    is_transient,
)
from engine.runner.paper_state import LiveSnapshot, PauseEntry, RunnerSnapshot, decode_blob, encode_blob
from engine.runner.selector_store import ScoreStore

log = structlog.get_logger()

SYSTEM = "system"
OCO_LEG = "oco"
PARTIAL_LEG = "partial"
EXIT_LEG = "x"
EMERGENCY_LEG = "emg"
# a foto de equity precede a fronteira do evento: trades no mesmo instante ainda contam como posteriores
_PHOTO_LAG = timedelta(microseconds=1)
LESSON_CACHE_SECONDS = 600
CALENDAR_RELOAD_SECONDS = 3600
_CAPITAL_TOLERANCE = Decimal("0.000001")
_BACKOFF_RESET_SECONDS = 60.0
PERSISTENCE_PAUSE = "persistencia indisponivel: "
_MAX_COMMAND_ATTEMPTS = 3
_SIGNED_BLIND_ALERT_CYCLES = 5
_MAX_PAUSE_REASONS = 20
_EXIT_ID_ATTEMPTS = 3


class RunnerInvariantError(RuntimeError):
    """O robo e o gateway divergiram de um jeito que nao se corrige em silencio: parar e recuperar."""


class FeedError(RuntimeError):
    """A fonte de mercado falhou; o supervisor reconecta com backoff."""


class BotState(StrEnum):
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    FAILED_PERSISTENCE = "FAILED_PERSISTENCE"
    HALTED = "HALTED"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"


@dataclass
class _Pending:
    approved: ApprovedEntry
    strategy: str
    confidence: float
    record: OrderRecord


@dataclass(frozen=True)
class _AiTicket:
    """Entrada ja aprovada pelo gate que aguarda o parecer da IA FORA do lock."""

    result: StrategySignal
    window: list[Candle]
    candle: Candle
    now: datetime
    insight: dict[str, Any] | None
    order_id: str


@dataclass
class _Live:
    entry_id: str
    symbol: str
    strategy: str
    risk_amount: Decimal
    gateway_stop: Decimal
    entry_order: OrderRecord
    position: ManagedPosition | None = None
    emergency: EmergencyExit | None = None
    partial_order: OrderRecord | None = None
    partial_fill: Fill | None = None
    closing: ExitFill | None = None
    exit_order: OrderRecord | None = None
    exit_seq: int = 0

    @property
    def partial_id(self) -> str | None:
        return None if self.partial_order is None else self.partial_order.client_order_id


class PaperRobot:
    def __init__(
        self,
        config: PaperRunConfig,
        *,
        run_id: str,
        journal: Journal,
        bus: EventBus,
        market: MarketDataGateway,
        strategies: Sequence[Strategy],
        rules: dict[str, SymbolRules],
        calendar: EventCalendar,
        clock: Clock,
        commands: CommandQueue | None = None,
        paths: RunPaths | None = None,
        code_version: str = "unknown",
        max_runtime: timedelta | None = None,
        liquidate_on_emergency: bool = False,
        quotes: Callable[[str], tuple[Decimal, Decimal] | None] | None = None,
        persist_retry_delays: Sequence[float] = DEFAULT_RETRY_DELAYS,
        signed_commands: SignedCommandSource | None = None,
        audit_sink: AuditSink | None = None,
        alerts: AlertDispatcher | None = None,
        config_source: Mapping[str, Any] | None = None,
        score_store: ScoreStore | None = None,
        backtest_samples: Sequence[TradeSample] = (),
        analyst: MarketAnalyst | None = None,
        ai_status: str = "desligada",
        lesson_source: LessonSource | None = None,
        lessons_job: Callable[[], Awaitable[object]] | None = None,
        lessons_interval_hours: int = 168,
        calendar_reload: Callable[[EventCalendar, datetime], EventCalendar] | None = None,
    ) -> None:
        if not strategies:
            raise ValueError("at least one strategy is required")
        self.config = config
        self.run_id = run_id
        self.journal = DurableJournal(
            journal, Outbox(None if paths is None else paths.outbox), persist_retry_delays
        )
        self.bus = bus
        self.market = market
        self.strategies = tuple(strategies)
        self.rules = rules
        self.calendar = calendar
        self._calendar_reload = calendar_reload
        self.clock = clock
        self.commands = commands
        self.signed_commands = signed_commands
        self.audit_sink = audit_sink
        self.alerts = alerts
        self.config_source: dict[str, Any] | None = None if config_source is None else dict(config_source)
        self._discards = DiscardLimiter()
        self._signed_blind = 0
        self._applied_version: int | None = None
        self.paths = paths
        self.code_version = code_version
        self.max_runtime = max_runtime
        self.risk = config.risk
        self.context = StrategyContext(taker_fee_rate=self.risk.taker_fee_rate)
        self.gateway: PaperGateway
        self.state: RiskState
        self.stop_event = asyncio.Event()
        self.stop_reason = "parada solicitada"
        self.failed = False
        self.started_at: datetime = clock.now()
        self._lock = asyncio.Lock()
        self._pending: dict[str, _Pending] = {}
        self._live: dict[str, _Live] = {}
        self._history: dict[str, deque[Candle]] = {
            s: deque(maxlen=config.history_window) for s in config.symbols
        }
        self._marks: dict[str, Decimal] = {}
        self._last_handled: dict[str, datetime] = {}
        self._seen_seq = 0
        self._pauses: dict[str, PauseEntry] = {}
        self._reconcile_suspended = False
        self._draining = False
        self._restore_issues: list[str] = []
        self._blind = False
        self._blind_since: datetime | None = None
        self._blind_escalated = False
        self._emergency_liquidate = liquidate_on_emergency
        self._due: dict[str, datetime] = {}
        self._bot_state = BotState.STARTING
        self._calendar_open_warned = False
        self._tasks: list[asyncio.Task[None]] = []
        self._quotes = quotes or self._gateway_quotes
        self._score_store = score_store
        self._backtest_samples = tuple(backtest_samples)
        self._scores: dict[ScoreKey, StrategyScore] = {}
        self.analyst = analyst if config.ai.enabled else None
        self._ai_status = ai_status if config.ai.enabled else "desligada"
        self._lesson_source = lesson_source
        self._lessons_job = lessons_job
        self._lessons_seconds = lessons_interval_hours * 3600
        self._lesson_cache: tuple[datetime, tuple[str, ...]] | None = None
        self._background: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------ ciclo de vida

    @property
    def bot_state(self) -> BotState:
        if self._bot_state in (BotState.STOPPING, BotState.STOPPED, BotState.STARTING):
            return self._bot_state
        if self.journal.down:
            return BotState.FAILED_PERSISTENCE
        if self.state.halted:
            return BotState.HALTED
        if self.state.paused or self._blind:
            return BotState.PAUSED
        return BotState.RUNNING

    @property
    def live_symbols(self) -> list[str]:
        return sorted(live.symbol for live in self._live.values())

    async def start(self) -> None:
        """Recupera o run (se existir) ou cria um novo; so entao o robo aceita velas e sinais."""
        now = self.clock.now()
        if not await self.journal.flush():
            raise PersistenceUnavailableError(
                f"{self.journal.pending_events} evento(s) no outbox e o banco segue indisponivel: "
                "nao e seguro iniciar"
            )
        recovery = await self.journal.load()
        resumed = recovery.run is not None and recovery.gateway_state is not None
        inherited: tuple[str, str] | None = None
        runner_pauses: tuple[PauseEntry, ...] = ()
        run = RunRecord(
            run_id=self.run_id,
            mode=JournalRunMode.PAPER,
            started_at=now,
            config_hash=self.config.config_hash(),
            code_version=self.code_version[:64],
        )
        if resumed and recovery.gateway_state is not None and recovery.risk_state is not None:
            paper_state, runner = decode_blob(recovery.gateway_state.state)
            runner_pauses = runner.pauses
            self.gateway = PaperGateway.restore(paper_state, self.market, self.clock)
            self.state = recovery.risk_state
            self._seen_seq = paper_state.fill_seq if runner.seen_seq is None else runner.seen_seq
            self._restore_lives(runner, recovery.open_positions)
            self._last_handled = dict(runner.handled)
            for key, opened in paper_state.last_candle_open.items():
                symbol = key.partition("|")[0]
                self._last_handled[symbol] = max(self._last_handled.get(symbol, opened), opened)
        else:
            self.gateway = PaperGateway(self.market, self.clock, self.config.gateway_config())
            self.state = RiskState.initial(self.config.capital, now)
            inherited = await self.journal.previous_halt()
        if resumed:
            self._restore_pauses(runner_pauses, now)
        self.journal.on_down = self._persistence_down
        self.journal.on_up = self._persistence_up
        event = ControlEvent(
            at=now,
            action=ControlAction.START,
            operator=SYSTEM,
            reason="recuperacao apos queda" if resumed else "inicio do run",
        )
        await self.journal.start_run(run, self._config_payload(), event)
        if inherited is not None:
            await self._inherit_halt(*inherited, now)
        log.info(
            "paper_start",
            run_id=self.run_id,
            resumed=resumed,
            symbols=self.config.symbols,
            timeframe=self.config.timeframe.value,
            capital=str(self.config.capital),
            calendar_coverage=self.calendar.coverage_problem(now) is None,
        )
        self._warn_calendar(now, at_start=True)
        if resumed:
            await self._cancel_orphans(recovery.open_orders, now)
            await self._cancel_pending_after_restart(now)
        await self._flag_restore_issues()
        await self._consume_commands(now)
        await self._bootstrap_history(now)
        async with self._lock:
            await self._process_fills()
            await self._place_exits()
            await self._reconcile(self.clock.now(), startup=True)
            await self._save_state(self.clock.now())
        await self._load_scores()
        self._bot_state = BotState.RUNNING
        self._due = {"scores": now}
        await self._publish_state("start")

    async def run(self) -> None:
        """Alimenta o robo ate `stop_event`; falha fatal (persistencia, invariante) encerra com excecao."""
        self._tasks = [asyncio.create_task(self._housekeeping_loop())]
        for symbol in self.config.symbols:
            self._tasks.append(
                asyncio.create_task(self._supervise(f"book:{symbol}", partial(self._book_loop, symbol)))
            )
            self._tasks.append(
                asyncio.create_task(self._supervise(f"candles:{symbol}", partial(self._candle_loop, symbol)))
            )
        waiter = asyncio.create_task(self.stop_event.wait())
        try:
            done, _ = await asyncio.wait([waiter, *self._tasks], return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if task is not waiter and (error := task.exception()) is not None:
                    self.failed = True
                    raise error
        finally:
            # cancela com o lock: nenhuma tarefa para no meio de um fill (estado e diario ficam coerentes)
            async with self._lock:
                for task in [waiter, *self._tasks]:
                    task.cancel()
                await asyncio.gather(waiter, *self._tasks, return_exceptions=True)

    def request_stop(self, reason: str) -> None:
        self.stop_reason = reason
        self.stop_event.set()

    async def shutdown(self, reason: str | None = None) -> None:
        """Parada ordenada: sem entradas novas, fills drenados, estado salvo; stops NUNCA sao removidos."""
        self._bot_state = BotState.STOPPING
        reason = reason or self.stop_reason
        if self.failed:
            # falha fatal (diario ou invariante): nao grava estado possivelmente incoerente; vale o ultimo
            # estado atomico do diario e a recuperacao reprocessa as velas perdidas
            log.critical("paper_failed_no_state_save", run_id=self.run_id, open_positions=self.live_symbols)
            self._bot_state = BotState.STOPPED
            await self._drain_alerts()
            await self.bus.close()
            return
        for task in list(self._background):
            task.cancel()
        async with self._lock:
            now = self.clock.now()
            if self.journal.down:
                await self.journal.flush()
            await self._process_fills()
            for pending in list(self._pending.values()):
                await self._cancel_entry(
                    pending, "shutdown", "parada ordenada: ordem de entrada cancelada", now
                )
            await self._save_state(now)
            await self.journal.record_control(
                ControlEvent(at=now, action=ControlAction.STOP, operator=SYSTEM, reason=reason),
                risk_state=self.state,
                gateway_state=self._blob(),
                audit={"open_positions": self.live_symbols},
            )
            if self.journal.pending_events and not await self.journal.flush():
                log.critical(
                    "outbox_pending_on_shutdown",
                    pending=self.journal.pending_events,
                    effect="sera reaplicado na proxima partida; a partida recusa se o banco seguir fora",
                )
            self._bot_state = BotState.STOPPED
            await self._publish_state("stop")
            self._write_status(now)
        log.info("paper_stopped", run_id=self.run_id, reason=reason, open_positions=self.live_symbols)
        await self._drain_alerts()
        await self.bus.close()

    async def _drain_alerts(self) -> None:
        if self.alerts is not None:
            await self.alerts.drain()

    # ------------------------------------------------------------------ alimentacao

    async def on_book(self, book: OrderBookSnapshot) -> None:
        async with self._lock:
            self.gateway.on_book(book)
            if book.bids and book.asks:
                self._marks[book.symbol] = (book.bids[0].price + book.asks[0].price) / 2
            await self._process_fills()
            await self._place_exits()

    async def on_candle(self, candle: Candle) -> None:
        if not candle.closed or candle.symbol not in self._history:
            return
        async with self._lock:
            tickets = await self._ingest(candle)
        for ticket in tickets:
            await self._finish_ai_entry(ticket)

    async def _supervise(self, name: str, factory: Callable[[], Awaitable[None]]) -> None:
        backoff_attempt = 0
        while True:
            started = asyncio.get_running_loop().time()
            try:
                await factory()
                raise FeedError("o fluxo terminou")
            except FeedError as error:
                ran = asyncio.get_running_loop().time() - started
                backoff_attempt = 0 if ran > _BACKOFF_RESET_SECONDS else backoff_attempt + 1
                delay = min(2.0 ** (backoff_attempt - 1), 60.0)
                log.error("feed_failed", feed=name, error=scrub_text(repr(error)), retry_in=delay)
                await asyncio.sleep(delay)

    async def _book_loop(self, symbol: str) -> None:
        source = self.gateway.stream_order_book(symbol, self.config.gateway_config().book_depth).__aiter__()
        timeout = float(max(self.config.max_stop_blind_seconds, 10))
        try:
            while True:
                try:
                    book = await asyncio.wait_for(source.__anext__(), timeout)
                except StopAsyncIteration:
                    raise FeedError(f"fluxo de book de {symbol} terminou") from None
                except TimeoutError:
                    raise FeedError(f"sem book de {symbol} ha {timeout:.0f}s") from None
                except Exception as error:  # qualquer falha da fonte: reconecta com backoff
                    raise FeedError(f"falha no fluxo de book de {symbol}: {error!r}") from error
                await self.on_book(book)
        finally:
            await _aclose(source)

    async def _candle_loop(self, symbol: str) -> None:
        last = self._last_handled.get(symbol)
        since = None if last is None else last + timedelta(seconds=self.config.timeframe.seconds)
        source = self.market.stream_candles(symbol, self.config.timeframe, since).__aiter__()
        try:
            while True:
                try:
                    candle = await source.__anext__()
                except StopAsyncIteration:
                    raise FeedError(f"fluxo de velas de {symbol} terminou") from None
                except Exception as error:  # qualquer falha da fonte: reconecta com backoff
                    raise FeedError(f"falha no fluxo de velas de {symbol}: {error!r}") from error
                await self.on_candle(candle)
        finally:
            await _aclose(source)

    async def _housekeeping_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                await self.housekeeping()
            except Exception as error:
                if not is_transient(error):
                    raise
                log.error("housekeeping_failed", error=scrub_text(repr(error)))
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.stop_event.wait(), 1.0)

    async def housekeeping(self) -> None:
        """Tarefas por tempo (comandos, saude, expiracao, equity, reconciliacao, heartbeat)."""
        now = self.clock.now()
        if self.journal.down:
            async with self._lock:
                await self.journal.flush()
        await self._consume_commands(now)
        async with self._lock:
            await self._process_fills()
            await self._place_exits()
            await self._expire_pending(now)
            await self._check_health(now)
            if self._is_due("equity", now, self.config.equity_seconds):
                await self._equity(now)
            if self._is_due("reconcile", now, self.config.reconcile_seconds):
                await self._reconcile(now)
            if self._is_due("heartbeat", now, self.config.heartbeat_seconds):
                await self._emit("heartbeat", self.status_payload(now))
                self._write_status(now)
        if self._calendar_reload is not None and self._is_due("calendar", now, CALENDAR_RELOAD_SECONDS):
            self.calendar = self._calendar_reload(self.calendar, now)
        if self.config.selector.enabled and self._is_due(
            "scores", now, self.config.selector.recalc_hours * 3600
        ):
            await self._recalculate_scores(now)
        if self._lessons_job is not None and self._is_due("lessons", now, self._lessons_seconds):
            self._spawn_lessons(self._lessons_job)
        if self.max_runtime is not None and now - self.started_at >= self.max_runtime:
            self.request_stop("tempo maximo de execucao atingido")

    def _spawn_lessons(self, job: Callable[[], Awaitable[object]]) -> None:
        """Diario de licoes em segundo plano: nunca bloqueia o ciclo e qualquer falha so vai para o log."""

        async def run() -> None:
            try:
                await job()
            except Exception as error:
                log.warning("lessons_job_failed", error=scrub_text(repr(error)))

        task = asyncio.create_task(run())
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _lesson_notes(self, now: datetime) -> tuple[str, ...]:
        """Licoes APROVADAS por admin (sanitizadas e limitadas); cache curto; falha => sem licoes."""
        source = self._lesson_source
        if source is None:
            return ()
        cached = self._lesson_cache
        if cached is not None and now - cached[0] < timedelta(seconds=LESSON_CACHE_SECONDS):
            return cached[1]
        try:
            notes = lesson_notes(await source.approved(MAX_LESSONS))
        except Exception as error:
            log.warning("lessons_load_failed", error=scrub_text(repr(error)))
            notes = ()
        self._lesson_cache = (now, notes)
        return notes

    def _is_due(self, key: str, now: datetime, seconds: int) -> bool:
        last = self._due.get(key)
        if last is not None and now - last < timedelta(seconds=seconds):
            return False
        self._due[key] = now
        return True

    # ------------------------------------------------------------------ historico e velas

    async def _bootstrap_history(self, now: datetime) -> None:
        step = timedelta(seconds=self.config.timeframe.seconds)
        replay: list[Candle] = []
        for symbol in self.config.symbols:
            end = _floor(now, self.config.timeframe.seconds)
            start = end - step * (self.config.history_window + 2)
            fetched = [
                c
                for c in await self.market.fetch_candles(symbol, self.config.timeframe, start, end + step)
                if c.closed and c.open_time < end
            ]
            fetched.sort(key=lambda c: c.open_time)
            resume_from = self._last_handled.get(symbol)
            history = self._history[symbol]
            for candle in fetched:
                if resume_from is not None and candle.open_time > resume_from:
                    replay.append(candle)
                else:
                    history.append(candle)
            if resume_from is None and fetched:
                self._last_handled[symbol] = fetched[-1].open_time
                self._marks[symbol] = fetched[-1].close
        for candle in sorted(replay, key=lambda c: (c.open_time, c.symbol)):
            async with self._lock:
                await self._handle_candle(candle, stale=True)
        if replay:
            log.info("paper_replay_done", candles=len(replay))

    async def _ingest(self, candle: Candle) -> list[_AiTicket]:
        symbol = candle.symbol
        last = self._last_handled.get(symbol)
        step = timedelta(seconds=self.config.timeframe.seconds)
        if last is not None:
            if candle.open_time <= last:
                return []
            if candle.open_time - last > step:
                for missing in await self._recover_gap(symbol, last + step, candle.open_time):
                    await self._handle_candle(missing, stale=True)
        lag = self.clock.now() - candle.close_time
        return await self._handle_candle(candle, stale=lag > timedelta(seconds=self.config.eval_lag_seconds))

    async def _recover_gap(self, symbol: str, start: datetime, end: datetime) -> list[Candle]:
        """Politica do M2: buraco de velas e recuperado por `fetch_candles`; sem sucesso, nao se opera."""
        try:
            fetched = await self.market.fetch_candles(symbol, self.config.timeframe, start, end)
        except Exception as error:  # a recuperacao e best-effort: a falha vira decisao 'nao operou'
            log.error("candle_gap_recovery_failed", symbol=symbol, error=scrub_text(repr(error)))
            return []
        recovered = sorted((c for c in fetched if c.closed and start <= c.open_time < end), key=_open_time)
        log.warning(
            "candle_gap_detected", symbol=symbol, missing_from=start.isoformat(), recovered=len(recovered)
        )
        return recovered

    async def _handle_candle(self, candle: Candle, *, stale: bool) -> list[_AiTicket]:
        symbol = candle.symbol
        now = candle.close_time if stale else max(self.clock.now(), candle.close_time)
        self.gateway.on_candle(candle)
        self._history[symbol].append(candle)
        self._marks[symbol] = candle.close
        self._last_handled[symbol] = candle.open_time
        await self._process_fills()
        await self._manage(symbol, candle)
        await self._set_state(self.state.roll_day(now), now)
        await self._photo(now)
        await self._expire_pending(now)
        await self._place_exits()
        await self._equity(now)
        tickets: list[_AiTicket] = []
        if stale:
            await self._decide_stale(candle)
        else:
            tickets = await self._evaluate(candle, now)
        await self._save_state(now)
        return tickets

    # ------------------------------------------------------------------ fills

    async def _process_fills(self) -> None:
        """Processa fills novos em ordem. `_seen_seq` so avanca DEPOIS que o evento foi gravado (diario ou
        outbox durável): uma falha no meio nunca marca como visto um fill que nao chegou ao diario."""
        if self._draining:
            return
        fresh = sorted((f for f in self.gateway.fills if f.sequence > self._seen_seq), key=_sequence)
        if not fresh:
            return
        stopped_now = {
            f.client_order_id.partition(":")[0] for f in fresh if f.client_order_id.endswith(f":{OCO_LEG}")
        }
        self._draining = True
        try:
            for fill in fresh:
                await self._handle_fill(fill, stopped_now)
                self._seen_seq = fill.sequence
        finally:
            self._draining = False

    async def _handle_fill(self, fill: Fill, stopped_now: set[str]) -> None:
        entry_id, _, leg = fill.client_order_id.partition(":")
        if fill.side == "buy":
            await self._on_entry_fill(fill, entry_id in stopped_now)
            return
        live = self._live.get(entry_id)
        if live is None:
            raise RunnerInvariantError(f"fill de venda sem posicao: {fill.client_order_id}")
        if leg == OCO_LEG:
            await self._on_protective_fill(live, fill)
        elif leg == PARTIAL_LEG:
            live.partial_fill = fill
        else:
            await self._on_exit_fill(live, fill, leg)

    async def _on_entry_fill(self, fill: Fill, stopped_now: bool) -> None:
        pending = self._pending.pop(fill.client_order_id, None)
        if pending is None:
            raise RunnerInvariantError(f"fill de compra sem ordem pendente: {fill.client_order_id}")
        approved = pending.approved.verify()
        opened_at = max(fill.at, approved.guards.approved_at)
        outcome = open_position(
            approved, opened_at=opened_at, fill_price=fill.price, filled_quantity=fill.quantity, fee=fill.fee
        )
        entry_id = fill.client_order_id
        filled = _filled(pending.record, fill)
        oco = OrderRecord.oco_of(pending.record, status=OrderStatus.NEW, at=fill.at)
        dust = isinstance(outcome, Rejection)
        if isinstance(outcome, Rejection):
            # o fill JA aconteceu: o saldo existe e precisa de dono. Vira uma venda de emergencia registrada
            # (se o gateway recusar o minimo, o robo fica pausado e alertado), nunca uma excecao que trava o
            # restart
            outcome = EmergencyExit(
                symbol=fill.symbol,
                quantity=fill.quantity,
                fill_price=fill.price,
                stop_loss=approved.order.stop_loss,
                entry_fee=fill.fee,
                opened_at=opened_at,
                reason=outcome.message,
            )
        if isinstance(outcome, EmergencyExit):
            live = _Live(
                entry_id,
                outcome.symbol,
                pending.strategy,
                approved.risk_amount,
                approved.order.stop_loss,
                pending.record,
                emergency=outcome,
            )
            self._live[entry_id] = live
            decision = self._decision(
                at=opened_at,
                symbol=outcome.symbol,
                action=DecisionAction.ENTER,
                traded=True,
                code="dust_fill" if dust else "emergency_exit",
                reason=f"fill fora do aprovado, venda a mercado imediata: {outcome.reason}",
                strategy=pending.strategy,
                confidence=pending.confidence,
                signal=approved.signal,
                order_id=entry_id,
            )
            log.error(
                "emergency_exit", symbol=outcome.symbol, reason=outcome.reason, quantity=str(outcome.quantity)
            )
            await self._alert(
                "dust_fill" if dust else "emergency_exit", f"{outcome.symbol}: {outcome.reason}"
            )
            await self.journal.record_entry(
                orders=[filled, oco],
                fill=_fill_record(fill, self.config.risk.quote_asset),
                position=None,
                risk_state=self.state,
                gateway_state=self._blob(fill.sequence),
                decision=decision,
            )
            self._mark_seen(fill)
            await self._emit("decisions", _decision_payload(decision))
            if dust:
                await self._pause(f"fill em poeira em {outcome.symbol}: {outcome.reason}")
            if not stopped_now:
                await self._place_exits()
            return
        live = _Live(
            entry_id,
            outcome.symbol,
            pending.strategy,
            approved.risk_amount,
            outcome.stop,
            pending.record,
            position=outcome,
        )
        self._live[entry_id] = live
        orders = [filled, oco]
        if not stopped_now:
            partial = await self._protect(live, outcome)
            if partial is not None:
                orders.append(partial)
        await self.journal.record_entry(
            orders=orders,
            fill=_fill_record(fill, self.config.risk.quote_asset),
            position=live.position,
            risk_state=self.state,
            gateway_state=self._blob(fill.sequence),
        )
        self._mark_seen(fill)
        log.info(
            "entry_filled",
            symbol=outcome.symbol,
            entry_id=entry_id,
            price=str(fill.price),
            quantity=str(fill.quantity),
            stop=str(outcome.stop),
        )
        await self._emit("orders", _fill_payload("entry_filled", fill, entry_id))
        await self._equity(self.clock.now())

    async def _protect(self, live: _Live, position: ManagedPosition) -> OrderRecord | None:
        """Stop puro no OCO; o alvo vira um limit de venda do tamanho da parcial de `manage_position`."""
        self.gateway.drop_target(live.entry_id)
        plan = plan_partial(position, self.risk, self.rules[live.symbol])
        if plan is None:
            live.position = position.model_copy(update={"partial_done": True, "take_profit": None})
            return None
        order = ExitOrder(
            client_order_id=f"{live.entry_id}:{PARTIAL_LEG}",
            symbol=live.symbol,
            type=OrderType.LIMIT,
            quantity=plan.quantity,
            price=plan.price,
            created_at=self.clock.now(),
        )
        report = await self.gateway.place_order(order)
        if report.status is OrderStatus.REJECTED:
            reason = self.gateway.rejection_reason(order.client_order_id)
            raise RunnerInvariantError(f"parcial recusada pelo gateway: {reason}")
        live.partial_order = OrderRecord.from_order(order)
        return live.partial_order

    async def _on_protective_fill(self, live: _Live, fill: Fill) -> None:
        await self._cancel_partial(live)
        await self._cancel_exit_order(live)
        if live.partial_fill is not None and live.position is not None:
            await self._apply_partial(live, live.partial_fill)
        reason = fill.exit_reason or ExitReason.STOP_LOSS
        oco = _filled(OrderRecord.oco_of(live.entry_order), fill)
        if live.position is not None:
            if fill.quantity != live.position.quantity:
                raise RunnerInvariantError("quantidade do stop diverge da posicao")
            at = max(fill.at, live.position.opened_at)
            trade = close_position(live.position, fill.price, at, reason, fee=fill.fee).to_trade(
                live.position
            )
        else:
            trade = self._emergency_trade(live, fill, reason)
        await self._finish(live, trade, fill, [oco])

    async def _on_exit_fill(self, live: _Live, fill: Fill, leg: str) -> None:
        record = live.exit_order
        if record is None:
            raise RunnerInvariantError(f"fill de saida inesperado: {fill.client_order_id}")
        if leg.startswith(EMERGENCY_LEG) and live.emergency is not None:
            trade = self._emergency_trade(live, fill, ExitReason.MANUAL)
        elif (
            live.closing is not None and live.position is not None and fill.quantity == live.closing.quantity
        ):
            at = max(fill.at, live.position.opened_at)
            closing = live.closing.model_copy(update={"price": fill.price, "fee": fill.fee, "at": at})
            trade = closing.to_trade(live.position)
        else:
            raise RunnerInvariantError(f"fill de saida inesperado: {fill.client_order_id}")
        # a venda a mercado consome o OCO no gateway: o diario encerra a linha do OCO na mesma transacao
        oco = OrderRecord.oco_of(
            live.entry_order, status=OrderStatus.CANCELED, at=max(fill.at, live.entry_order.created_at)
        )
        await self._finish(live, trade, fill, [_filled(record, fill), oco])

    def _emergency_trade(self, live: _Live, fill: Fill, reason: ExitReason) -> Trade:
        if live.emergency is None:
            raise RunnerInvariantError("trade de emergencia sem EmergencyExit")
        return emergency_trade(
            live.emergency,
            trade_id=f"{live.entry_id}:{EMERGENCY_LEG}",
            price=fill.price,
            fee=fill.fee,
            at=fill.at,
            reason=reason,
        )

    async def _apply_partial(
        self,
        live: _Live,
        fill: Fill,
        mgmt: ExitFill | None = None,
        after: ManagedPosition | None = None,
    ) -> None:
        """Parcial executada pelo gateway: vira trade e reduz a posicao.

        `mgmt` e o ExitFill previsto pela gestao (preco/taxa/hora reais vem do fill); `after` e a posicao ja
        atualizada pela gestao (stop, maxima), usada no lugar da simples reducao de quantidade.
        """
        position = live.position
        if position is None:
            raise RunnerInvariantError("parcial sem posicao")
        at = max(fill.at, position.opened_at)
        if mgmt is None:
            exit_fill = ExitFill(
                fill_id=exit_fill_id(position, at, ExitReason.TAKE_PROFIT),
                quantity=fill.quantity,
                price=fill.price,
                reason=ExitReason.TAKE_PROFIT,
                at=at,
                fee=fill.fee,
            )
        else:
            exit_fill = mgmt.model_copy(update={"price": fill.price, "fee": fill.fee, "at": at})
        trade = exit_fill.to_trade(position)
        reduced = after or position.model_copy(
            update={
                "quantity": position.quantity - fill.quantity,
                "partial_done": True,
                "take_profit": None,
                "realized_pnl": position.realized_pnl + trade.pnl,
            }
        )
        order = live.partial_order
        records = [] if order is None else [_filled(order, fill)]
        live.partial_fill = None
        live.partial_order = None
        live.position = reduced
        await self._leg(live, trade, fill, reduced, records)

    async def _finish(self, live: _Live, trade: Trade, fill: Fill, orders: list[OrderRecord]) -> None:
        del self._live[live.entry_id]
        await self._leg(live, trade, fill, None, orders)

    async def _leg(
        self,
        live: _Live,
        trade: Trade,
        fill: Fill,
        position: ManagedPosition | None,
        orders: list[OrderRecord],
    ) -> None:
        # o HALT causado por este trade viaja na MESMA transacao do trade: nunca estado/evento sem a perda
        halt = await self._apply_state(
            self.state.record_trade(trade, trade.closed_at, self.risk), trade.closed_at
        )
        await self.journal.record_exit(
            orders=orders,
            fill=_fill_record(fill, self.config.risk.quote_asset),
            trade=trade,
            position=position,
            risk_state=self.state,
            strategy=live.strategy,
            risk_amount=live.risk_amount,
            gateway_state=self._blob(fill.sequence),
            without_position_row=live.emergency is not None and live.position is None,
            halt=halt,
        )
        self._mark_seen(fill)
        if halt is not None:
            await self._announce_halt(halt)
        log.info(
            "trade_closed",
            symbol=trade.symbol,
            trade_id=trade.trade_id,
            reason=trade.exit_reason.value,
            pnl=str(trade.pnl),
            equity=str(self.state.equity),
        )
        await self._emit("trades", {"trade": trade.model_dump(mode="json"), "pnl": str(trade.pnl)})
        await self._emit("orders", _fill_payload("exit_filled", fill, live.entry_id))
        await self._equity(self.clock.now())

    # ------------------------------------------------------------------ gestao da posicao

    async def _manage(self, symbol: str, candle: Candle) -> None:
        for live in [lv for lv in self._live.values() if lv.symbol == symbol]:
            position = live.position
            if position is None or live.closing is not None or candle.open_time < position.opened_at:
                continue
            result = manage_position(position, candle, self.rules[symbol], self.risk)
            exits = list(result.exits)
            partial = next((x for x in exits if x.reason is ExitReason.TAKE_PROFIT), None)
            if partial is None and live.partial_fill is not None:
                log.warning("partial_filled_without_management", symbol=symbol)
                await self._apply_partial(live, live.partial_fill)
                continue
            if partial is not None and live.partial_fill is None:
                log.warning("partial_pending_in_gateway", symbol=symbol)
                continue
            updated = result.position
            if (
                updated is not None
                and result.stop_update is not None
                and result.stop_update > live.gateway_stop
            ):
                updated = self._raise_stop(live, updated, result.stop_update)
            if partial is not None and live.partial_fill is not None:
                exits.remove(partial)
                await self._apply_partial(live, live.partial_fill, partial, updated)
            if result.position is None:
                await self._begin_exit(live, candle, exits)
            elif updated is not None and updated != live.position:
                live.position = updated
                await self.journal.record_position(updated, self._blob())

    def _raise_stop(self, live: _Live, position: ManagedPosition, new_stop: Decimal) -> ManagedPosition:
        """Sobe o stop no gateway; o stop da posicao nunca passa do que o gateway realmente tem."""
        try:
            self.gateway.raise_stop(live.entry_id, new_stop)
        except (ValueError, UnknownOrderError) as error:
            log.warning("raise_stop_deferred", symbol=live.symbol, wanted=str(new_stop), error=str(error))
            return position.model_copy(update={"stop": live.gateway_stop})
        live.gateway_stop = new_stop
        return position

    async def _begin_exit(self, live: _Live, candle: Candle, exits: list[ExitFill]) -> None:
        position = live.position
        if position is None or not exits:
            raise RunnerInvariantError("gestao encerrou a posicao sem saida")
        if candle.low <= position.stop or len(exits) != 1:
            log.critical("stop_not_executed_by_gateway", symbol=live.symbol, stop=str(position.stop))
            await self._pause("gestao pede saida por stop que o gateway nao executou")
        live.closing = exits[-1]
        await self._cancel_partial(live)
        await self._place_exits()

    async def _place_exits(self) -> None:
        """Vende a mercado o que a gestao/emergencia mandou sair; sem cotacao, tenta no proximo tick."""
        for live in list(self._live.values()):
            if live.exit_order is not None:
                continue
            if live.emergency is not None:
                quantity, leg = live.emergency.quantity, EMERGENCY_LEG
            elif live.closing is not None:
                quantity, leg = live.closing.quantity, EXIT_LEG
            else:
                continue
            if self.gateway.best_prices(live.symbol) is None:
                continue
            await self._cancel_partial(live)
            placed = await self._submit_exit(live, leg, quantity)
            if placed is None:
                continue
            report, order = placed
            if report.status is OrderStatus.REJECTED:
                reason = self.gateway.rejection_reason(order.client_order_id)
                log.warning("market_exit_rejected", symbol=live.symbol, reason=reason)
                continue
            live.exit_order = OrderRecord.from_order(order)
            # linha da ordem e estado do gateway (com `exit_seq`) na MESMA transacao: uma queda entre os dois
            # deixaria o id gravado e o estado antigo, e o restart reusaria o id com outro `created_at`
            await self.journal.update_order(live.exit_order, gateway_state=self._settled_blob())
            await self._emit(
                "orders", {"event": "exit_placed", "order": live.exit_order.model_dump(mode="json")}
            )

    async def _submit_exit(
        self, live: _Live, leg: str, quantity: Decimal
    ) -> tuple[OrderReport, ExitOrder] | None:
        """Id de saida unico e crescente por posicao; id ja usado (gateway restaurado) pula para o proximo."""
        for _ in range(_EXIT_ID_ATTEMPTS):
            live.exit_seq += 1
            order = ExitOrder(
                client_order_id=f"{live.entry_id}:{leg}{live.exit_seq}",
                symbol=live.symbol,
                type=OrderType.MARKET,
                quantity=quantity,
                created_at=self.clock.now(),
            )
            try:
                return await self.gateway.place_order(order), order
            except DuplicateOrderError:
                log.warning("exit_order_id_taken", order=order.client_order_id)
        log.error("exit_order_ids_exhausted", entry=live.entry_id)
        return None

    async def _cancel_partial(self, live: _Live) -> None:
        order = live.partial_order
        if order is None or live.partial_fill is not None:
            return
        report = await self._cancel_order(live.symbol, order.client_order_id)
        if report is not None and report.status is OrderStatus.CANCELED:
            live.partial_order = None
            await self.journal.update_order(
                order.model_copy(update={"status": OrderStatus.CANCELED}), gateway_state=self._settled_blob()
            )

    async def _cancel_exit_order(self, live: _Live) -> None:
        order = live.exit_order
        if order is None:
            return
        report = await self._cancel_order(live.symbol, order.client_order_id)
        live.exit_order = None
        if report is not None and report.status is OrderStatus.CANCELED:
            await self.journal.update_order(
                order.model_copy(update={"status": OrderStatus.CANCELED}), gateway_state=self._settled_blob()
            )

    async def _cancel_order(self, symbol: str, client_order_id: str) -> OrderReport | None:
        """Unico ponto de cancelamento do robo: o OCO (stop) NUNCA e cancelado em operacao normal."""
        if client_order_id.endswith(f":{OCO_LEG}"):
            raise RunnerInvariantError(f"cancelar OCO e proibido: {client_order_id}")
        try:
            return await self.gateway.cancel_order(symbol, client_order_id)
        except UnknownOrderError:
            return None

    # ------------------------------------------------------------------ entradas

    async def _evaluate(self, candle: Candle, now: datetime) -> list[_AiTicket]:
        symbol = candle.symbol
        window = list(self._history[symbol])
        timeframe = self.config.timeframe
        problem = history_problem(window, symbol, timeframe, now, self.risk.min_history_candles)
        if problem is not None and await self._refresh_history(symbol):
            window = list(self._history[symbol])
            problem = history_problem(window, symbol, timeframe, now, self.risk.min_history_candles)
        if problem is not None:
            await self._record_candle_decision(
                candle, "history_gap", f"historico com problema, nao opera: {problem}"
            )
            return []
        strategies: Sequence[Strategy] = self.strategies
        insight: dict[str, Any] | None = None
        if self.config.selector.enabled:
            selected = await self._select(candle, window)
            if selected is None:
                return []
            strategies, insight = selected
        found: list[StrategySignal] = []
        for strategy in strategies:
            if (signal := strategy.evaluate(window, self.context)) is not None:
                found.append(signal)
        if not found:
            names = ", ".join(s.name for s in strategies)
            await self._record_candle_decision(
                candle,
                "no_signal",
                f"nenhuma estrategia sinalizou entrada ({names})",
                insight=insight,
                close=str(candle.close),
            )
            return []
        found.sort(key=lambda s: (-s.confidence, s.signal.strategy))
        tickets: list[_AiTicket] = []
        for index, result in enumerate(found):
            if (ticket := await self._decide(result, window, candle, now, index, insight)) is not None:
                tickets.append(ticket)
        return tickets

    async def _select(
        self, candle: Candle, window: list[Candle]
    ) -> tuple[list[Strategy], dict[str, Any]] | None:
        """Estrategias liberadas pelo seletor nesta vela; None (com uma decisao de nao operar) se nenhuma."""
        if not self._scores:
            log.warning(
                "selector_no_scores", symbol=candle.symbol, effect="sem scores salvos: o robo nao opera"
            )
            await self._record_candle_decision(
                candle,
                SELECTOR_NO_SCORES,
                "Seletor de estratégias ligado, mas sem scores salvos: o robô não opera até que sejam "
                "publicados (python -m engine.runner.validate --publish-scores).",
            )
            return None
        selector = self.config.selector
        reading = classify_regime(window, selector.regime)
        decision = select_for_reading(
            reading, candle.symbol, [s.name for s in self.strategies], self._scores, selector.selector
        )
        insight = selection_insight(decision)
        if not decision.operate:
            await self._record_candle_decision(candle, SELECTOR_IDLE, decision.reason, insight=insight)
            return None
        chosen = {c.strategy for c in decision.chosen}
        return [s for s in self.strategies if s.name in chosen], insight

    async def _load_scores(self) -> None:
        if not self.config.selector.enabled:
            return
        if self._score_store is None:
            log.error("selector_without_store", effect="seletor ligado sem repositorio de scores: nao opera")
            return
        try:
            self._scores = await self._score_store.latest_scores(
                self.config.timeframe.value, self.config.symbols
            )
        except Exception as error:  # sem scores o robo simplesmente nao opera
            log.error("selector_scores_load_failed", error=scrub_text(repr(error)))
            self._scores = {}
        if self._scores:
            log.info("selector_scores_loaded", keys=len(self._scores))
        else:
            log.warning("selector_no_scores", effect="nenhum snapshot de scores salvo: o robo nao opera")

    async def _recalculate_scores(self, now: datetime) -> None:
        """Scores novos de (trades do diario + amostras de backtest importadas); snapshot append-only."""
        store, selector = self._score_store, self.config.selector
        if store is None:
            return
        try:
            symbols = set(self.config.symbols)
            pool = [s for s in self._backtest_samples if s.symbol in symbols]
            pool += await store.journal_samples(self.config.symbols)
            if not pool:
                return
            scores = build_scores(pool, now, selector.selector, lookback_days=selector.lookback_days)
            await store.publish(
                list(scores.values()),
                timeframe=self.config.timeframe.value,
                origin="paper",
                calculated_at=now,
                run_id=self.run_id,
            )
            self._scores = scores
            log.info("selector_scores_recalculated", keys=len(scores), samples=len(pool))
        except Exception as error:  # mantem os scores anteriores
            log.error("selector_scores_recalc_failed", error=scrub_text(repr(error)))

    def _skipper(
        self, result: StrategySignal, candle: Candle, insight: dict[str, Any] | None
    ) -> Callable[[str, str], Awaitable[None]]:
        signal = result.signal

        async def skip(code: str, message: str) -> None:
            await self._record(
                self._decision(
                    at=candle.close_time,
                    symbol=signal.symbol,
                    action=DecisionAction.SKIP,
                    traded=False,
                    code=code,
                    reason=message,
                    strategy=signal.strategy,
                    confidence=result.confidence,
                    signal=signal,
                    insight=insight,
                )
            )

        return skip

    async def _decide(
        self,
        result: StrategySignal,
        window: list[Candle],
        candle: Candle,
        now: datetime,
        index: int = 0,
        insight: dict[str, Any] | None = None,
    ) -> _AiTicket | None:
        """Sob o lock: gate e (sem IA) ordem. Com IA devolve o ticket; o parecer sai do lock."""
        skip = self._skipper(result, candle, insight)
        order_id = _entry_id(candle, index)
        outcome = await self._gate(result.signal, window, now, order_id, skip)
        if outcome is None:
            return None
        if self.analyst is not None:
            return _AiTicket(result, window, candle, now, insight, order_id)
        await self._place_entry(outcome, result, candle, order_id, insight, skip)
        return None

    async def _finish_ai_entry(self, ticket: _AiTicket) -> None:
        """Consulta a IA SEM o lock (nao trava book/fills) e so entao reentra para revalidar e enviar a
        ordem. Cancelamento propaga sem tocar no lock; parada em curso descarta a entrada."""
        result, candle, order_id = ticket.result, ticket.candle, ticket.order_id
        if self.stop_event.is_set() or self._bot_state is not BotState.RUNNING:
            log.info("ai_entry_dropped_on_stop", order_id=order_id)
            return
        opinion = await self._consult_ai(result, ticket.window, ticket.insight, order_id)
        insight = ticket.insight
        if opinion is not None:
            insight = {**(insight or {}), "ai_opinion": opinion.summary(), "ai_detail": opinion.to_insight()}
        async with self._lock:
            if self.stop_event.is_set() or self._bot_state is not BotState.RUNNING:
                log.info("ai_entry_dropped_on_stop", order_id=order_id)
                return
            skip = self._skipper(result, candle, insight)
            combined = combine(result.confidence, opinion, min_confidence=self.config.ai_min_confidence)
            if not combined.approved and opinion is not None and combined.code is not None:
                await skip(
                    combined.code, _ai_reason(combined.code, opinion, combined.confidence, self.config)
                )
                return
            outcome = await self._gate(
                result.signal, ticket.window, max(ticket.now, self.clock.now()), order_id, skip
            )
            if outcome is None:
                return
            await self._place_entry(outcome, result, candle, order_id, insight, skip)
            await self._save_state(self.clock.now())

    async def _place_entry(
        self,
        outcome: ApprovedEntry,
        result: StrategySignal,
        candle: Candle,
        order_id: str,
        insight: dict[str, Any] | None,
        skip: Callable[[str, str], Awaitable[None]],
    ) -> None:
        signal = result.signal
        name = signal.strategy
        approved = outcome.verify()
        try:
            report = await self.gateway.place_order(approved.order)
        except DuplicateOrderError:
            await skip("duplicate_order", f"ordem {order_id} ja existe no gateway: sinal nao reenviado")
            return
        if report.status is OrderStatus.REJECTED:
            reason = self.gateway.rejection_reason(order_id) or "recusada"
            await skip("gateway_rejected", f"gateway recusou a ordem: {reason}")
            return
        record = OrderRecord.from_order(approved.order)
        self._pending[order_id] = _Pending(approved, name, result.confidence, record)
        decision = self._decision(
            at=candle.close_time,
            symbol=signal.symbol,
            action=DecisionAction.ENTER,
            traded=True,
            code="approved",
            reason=approved.to_decision().reason,
            strategy=name,
            confidence=result.confidence,
            signal=signal,
            order_id=order_id,
            insight=insight,
        )
        await self._record(decision, order=record, with_state=True)
        await self._emit("orders", {"event": "entry_placed", "order": record.model_dump(mode="json")})

    def _gateway_quotes(self, symbol: str) -> tuple[Decimal, Decimal] | None:
        return self.gateway.best_prices(symbol)

    def _calendar_mode(self, now: datetime) -> RunMode:
        if self.calendar.coverage_problem(now) is None:
            return "live"
        return "backtest" if self.config.allow_open_calendar else "live"

    def _warn_calendar(self, now: datetime, *, at_start: bool = False) -> None:
        problem = self.calendar.coverage_problem(now)
        if problem is None:
            return
        if self.config.allow_open_calendar:
            log.warning(
                "CALENDAR_OPEN_PAPER_MODE",
                at_start=at_start,
                problem=problem,
                warning="paper SEM calendario de eventos: sem protecao contra eventos de volatilidade",
            )
        elif at_start:
            log.error(
                "event_calendar_without_coverage",
                problem=problem,
                warning="toda entrada sera recusada (falha fechada); use --allow-open-calendar so no paper",
            )

    async def _refresh_history(self, symbol: str) -> bool:
        """Rebusca a janela inteira por REST quando ha buraco; so substitui se vier continua."""
        step = timedelta(seconds=self.config.timeframe.seconds)
        end = _floor(self.clock.now(), self.config.timeframe.seconds)
        try:
            fetched = await self.market.fetch_candles(
                symbol, self.config.timeframe, end - step * (self.config.history_window + 2), end + step
            )
        except Exception as error:  # best-effort
            log.error("history_refresh_failed", symbol=symbol, error=scrub_text(repr(error)))
            return False
        closed = sorted((c for c in fetched if c.closed and c.open_time < end), key=_open_time)
        last = self._history[symbol][-1].open_time if self._history[symbol] else None
        if not closed or (last is not None and closed[-1].open_time < last):
            return False
        history = self._history[symbol]
        history.clear()
        history.extend(closed)
        return True

    async def _decide_stale(self, candle: Candle) -> None:
        if await self.journal.has_decision(candle.symbol, candle.close_time):
            return
        await self._record_candle_decision(
            candle,
            "catch_up",
            "vela reprocessada apos queda ou atraso: gestao de posicao aplicada, nenhuma entrada avaliada",
        )

    async def _gate(
        self,
        signal: Signal,
        window: list[Candle],
        now: datetime,
        order_id: str,
        skip: Callable[[str, str], Awaitable[None]],
    ) -> ApprovedEntry | None:
        """Pre-condicoes + `evaluate_entry`; registra o motivo e devolve None se a entrada nao passa. Roda de
        novo depois do parecer da IA (que espera sem o lock): o estado e a cotacao podem ter mudado."""
        symbol = signal.symbol
        if any(live.emergency is not None for live in self._live.values()):
            await skip("emergency_exit_pending", "venda de emergencia em andamento")
            return None
        if self._blind:
            await skip("stops_blind", "stops cegos (sem dados de mercado): novas entradas pausadas")
            return None
        prices = self._quotes(symbol)
        if prices is None:
            await skip("no_quote", "sem cotacao fresca do book para validar a entrada")
            return None
        market = MarketSnapshot(
            symbol=symbol,
            at=min(self.clock.now(), now),
            bid=prices[0],
            ask=prices[1],
            quote_volume_24h=rolling_quote_volume(window, self.config.timeframe),
        )
        outcome = evaluate_entry(
            signal,
            state=self.state,
            config=self.risk,
            rules=self.rules[symbol],
            positions=[p for live in self._live.values() if (p := live.position) is not None],
            pending=[p.approved for p in self._pending.values()],
            market=market,
            history=window,
            calendar=self.calendar,
            now=now,
            client_order_id=order_id,
            mode=self._calendar_mode(now),
        )
        if isinstance(outcome, Rejection):
            await skip(outcome.code.value, outcome.message)
            return None
        return outcome

    async def _consult_ai(
        self,
        result: StrategySignal,
        window: list[Candle],
        insight: dict[str, Any] | None,
        order_id: str,
    ) -> AnalystOpinion | None:
        """Parecer da IA sobre um sinal que o gate JA aprovou. Chamada FORA do lock (nao trava book/fills);
        qualquer falha vira "sem parecer": o robo segue exatamente como sem IA."""
        analyst = self.analyst
        if analyst is None:
            return None
        signal = result.signal
        scores = [
            (str(e.get("strategy")), float(e["score"]))
            for e in (insight or {}).get("scores", [])
            if isinstance(e, dict) and isinstance(e.get("score"), int | float)
        ]
        try:
            request = build_request(
                window,
                result,
                scores=scores,
                regime=(insight or {}).get("regime") or None,
                lessons=await self._lesson_notes(self.clock.now()),
            )
            key = f"{signal.strategy}:{signal.symbol}:{signal.created_at:%y%m%d%H%M}"
            limit = self.config.ai.timeout_seconds * 2 + 5
            return await asyncio.wait_for(analyst.analyze(request, decision_key=key), timeout=limit)
        except Exception as error:
            log.warning("ai_consult_failed", order_id=order_id, error=scrub_text(repr(error)))
            return None

    async def _record_candle_decision(
        self,
        candle: Candle,
        code: str,
        reason: str,
        *,
        insight: dict[str, Any] | None = None,
        **context: Any,
    ) -> None:
        await self._record(
            self._decision(
                at=candle.close_time,
                symbol=candle.symbol,
                action=DecisionAction.SKIP,
                traded=False,
                code=code,
                reason=reason,
                insight=insight,
                context={"candle_open": candle.open_time.isoformat(), **context},
            )
        )

    def _decision(
        self,
        *,
        at: datetime,
        symbol: str,
        action: DecisionAction,
        traded: bool,
        code: str,
        reason: str,
        strategy: str | None = None,
        confidence: float | None = None,
        signal: Signal | None = None,
        order_id: str | None = None,
        insight: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> DecisionEntry:
        return DecisionEntry(
            decided_at=at,
            symbol=symbol,
            action=action,
            traded=traded,
            code=code,
            reason=reason,
            strategy=strategy,
            confidence=confidence,
            signal_id=None
            if signal is None
            else f"{signal.strategy}:{symbol}:{signal.created_at:%y%m%d%H%M}",
            client_order_id=order_id,
            signal=signal,
            insight=insight,
            context={
                "calendar_open": self.config.allow_open_calendar
                and self.calendar.coverage_problem(at) is not None,
                **(context or {}),
            },
        )

    async def _record(
        self, entry: DecisionEntry, *, order: OrderRecord | None = None, with_state: bool = False
    ) -> None:
        await self.journal.record_decision(
            entry, order=order, gateway_state=self._blob() if with_state else None
        )
        if entry.context.get("calendar_open"):
            log.warning("CALENDAR_OPEN_PAPER_MODE", decision=entry.code, symbol=entry.symbol)
        log.info(
            "decision",
            symbol=entry.symbol,
            code=entry.code,
            traded=entry.traded,
            strategy=entry.strategy,
            reason=entry.reason,
        )
        await self._emit("decisions", _decision_payload(entry))

    # ------------------------------------------------------------------ pendentes, estado, equity

    async def _expire_pending(self, now: datetime) -> None:
        _, expired = expire_pending(tuple(p.approved for p in self._pending.values()), now)
        for approved in expired:
            pending = self._pending.get(approved.order.client_order_id)
            if pending is not None:
                await self._cancel_entry(
                    pending, "entry_expired", "ordem limite aprovada nao executou na validade", now
                )

    async def _cancel_entry(self, pending: _Pending, code: str, reason: str, now: datetime) -> None:
        order = pending.approved.order
        report = await self._cancel_order(order.symbol, order.client_order_id)
        if report is None or report.status is not OrderStatus.CANCELED:
            await self._process_fills()
            return
        self._pending.pop(order.client_order_id, None)
        canceled = pending.record.model_copy(
            update={"status": OrderStatus.CANCELED, "updated_at": max(now, pending.record.created_at)}
        )
        await self._record(
            self._decision(
                at=now,
                symbol=order.symbol,
                action=DecisionAction.SKIP,
                traded=False,
                code=code,
                reason=reason,
                strategy=pending.strategy,
                confidence=pending.confidence,
                signal=pending.approved.signal,
                order_id=order.client_order_id,
            ),
            order=canceled,
            with_state=True,
        )

    async def _photo(self, now: datetime) -> None:
        """Foto autoritativa do capital: so sem posicao aberta (todos os trades ja registrados)."""
        if self._live:
            return
        balances = await self.gateway.get_balances()
        quote = balances[self.risk.quote_asset]
        cash = quote.free + quote.locked
        if abs(cash - self.state.equity) > _CAPITAL_TOLERANCE:
            await self._pause(f"capital do gateway {cash} diverge do ledger {self.state.equity}")
            return
        await self._set_state(self.state.observe_equity(cash, now - _PHOTO_LAG, self.risk), now)

    async def _apply_state(self, new: RiskState, now: datetime) -> ControlEvent | None:
        """Troca o estado em memoria; devolve o evento HALT se ESTA transicao desligou o robo."""
        previous = self.state
        self.state = new
        for entry in new.audit[len(previous.audit) :]:
            log.warning("risk_event", action=entry.action.value, detail=entry.detail, operator=entry.operator)
            await self._emit("risk", entry.model_dump(mode="json"))
            if entry.action.value == "daily_loss_pause":
                self._notify("daily_loss_pause", entry.detail)
        if new.halted and not previous.halted:
            return ControlEvent(
                at=now,
                action=ControlAction.HALT,
                operator=SYSTEM,
                reason=new.halt_reason or "drawdown maximo",
            )
        return None

    async def _announce_halt(self, event: ControlEvent) -> None:
        await self._alert("halted", event.reason)
        await self._publish_state("halted")

    async def _set_state(self, new: RiskState, now: datetime) -> None:
        halt = await self._apply_state(new, now)
        if halt is not None:
            await self._alert("halted", halt.reason)
            await self.journal.record_control(halt, risk_state=self.state, gateway_state=self._blob())
            await self._publish_state("halted")

    async def _inherit_halt(self, previous_run: str, reason: str, now: datetime) -> None:
        """Run novo herda o desligamento do anterior: nasce HALTED e so um `reactivate` explicito liga."""
        detail = f"herdado do run {previous_run}: {reason}"
        entry = AuditEntry(at=now, action=AuditAction.HALTED, detail=detail)
        self.state = self.state.model_copy(
            update={"halted": True, "halt_reason": detail, "audit": (*self.state.audit, entry)}
        )
        log.critical("halt_inherited", previous_run=previous_run, reason=reason)
        await self.journal.record_control(
            ControlEvent(at=now, action=ControlAction.HALT, operator=SYSTEM, reason=detail),
            risk_state=self.state,
            gateway_state=self._blob(),
        )
        await self._alert("halt_inherited", detail)

    async def _pause(
        self, reason: str, *, operator: str = SYSTEM, audit: dict[str, Any] | None = None
    ) -> bool:
        """Registra um motivo de pausa (conjunto, nao flag): devolve False se o motivo ja estava ativo."""
        if reason in self._pauses or len(self._pauses) >= _MAX_PAUSE_REASONS:
            log.warning("pause_reason_ignored", reason=reason, active=len(self._pauses))
            return False
        now = self.clock.now()
        self._pauses[reason] = PauseEntry(reason=reason, operator=operator, at=now)
        self.state = self.state.pause(reason, now)
        log.error("robot_paused", reason=reason, operator=operator, active=len(self._pauses))
        await self.journal.record_control(
            ControlEvent(at=now, action=ControlAction.PAUSE, operator=operator, reason=reason),
            risk_state=self.state,
            gateway_state=self._blob(),
            audit=audit,
        )
        await self._alert("paused", reason)
        await self._publish_state("paused")
        return True

    def _restore_pauses(self, saved: Sequence[PauseEntry], now: datetime) -> None:
        """Partida apos queda: so o motivo 'banco fora' se resolve sozinho (o outbox ja foi reaplicado)."""
        if self.state.paused:
            entries = list(saved) or [
                PauseEntry(reason=self.state.pause_reason or "pausa anterior", operator=SYSTEM, at=now)
            ]
            self._pauses = {e.reason: e for e in entries if not e.reason.startswith(PERSISTENCE_PAUSE)}
        if self.state.paused and not self._pauses:
            self.state = self.state.resume(
                operator=SYSTEM, reason="outbox reaplicado e banco disponivel na partida", now=now
            )
        elif self._pauses:
            self.state = self.state.model_copy(update={"pause_reason": next(iter(self._pauses))})

    def _mtm_equity(self) -> Decimal:
        equity = self.state.equity
        fee = self.risk.taker_fee_rate
        for live in self._live.values():
            mark = self._marks.get(live.symbol)
            if live.position is not None:
                price = mark or live.position.entry_price
                equity += live.position.unrealized_pnl(price) - live.position.quantity * price * fee
            elif live.emergency is not None:
                price = mark or live.emergency.fill_price
                gross = (price - live.emergency.fill_price) * live.emergency.quantity
                equity += gross - live.emergency.entry_fee - live.emergency.quantity * price * fee
        return equity

    async def _equity(self, now: datetime) -> None:
        point = EquityPoint(
            at=now, equity=self._mtm_equity(), realized=self.state.equity, in_position=bool(self._live)
        )
        if await self.journal.record_equity(point):
            await self._emit("equity", point.model_dump(mode="json"))

    # ------------------------------------------------------------------ saude e reconciliacao

    async def _check_health(self, now: datetime) -> None:
        health: PaperHealth = self.gateway.health(now)
        if health.stops_blind != self._blind:
            self._blind = health.stops_blind
            if self._blind:
                self._blind_since, self._blind_escalated = now, False
                ages = {s: int(a.total_seconds()) for s, a in health.stale.items()}
                log.error("STOPS_BLIND", stale_seconds=ages, effect="novas entradas pausadas")
                await self._alert("stops_blind", f"sem dados de mercado: {ages}")
                if not self.state.paused:
                    await self.journal.record_control(
                        ControlEvent(
                            at=now, action=ControlAction.PAUSE, operator=SYSTEM, reason="stops cegos"
                        ),
                        gateway_state=self._blob(),
                    )
            else:
                self._blind_since = None
                log.warning("stops_blind_cleared")
                if not self.state.paused:
                    await self.journal.record_control(
                        ControlEvent(
                            at=now,
                            action=ControlAction.RESUME,
                            operator=SYSTEM,
                            reason="dados de mercado voltaram",
                        ),
                        gateway_state=self._blob(),
                    )
            await self._publish_state("stops_blind")
        elif (
            self._blind
            and self._live
            and not self._blind_escalated
            and self._blind_since is not None
            and now - self._blind_since >= timedelta(seconds=self.config.blind_alert_seconds)
        ):
            self._blind_escalated = True
            positions = self.live_symbols
            log.critical(
                "STOPS_BLIND_PROLONGED", positions=positions, risk="no paper o stop so executa com dados"
            )
            await self._alert(
                "stops_blind_prolonged",
                f"posicoes {positions} sem dados de mercado ha mais de {self.config.blind_alert_seconds}s",
            )
            await self.journal.record_control(
                ControlEvent(
                    at=now,
                    action=ControlAction.PAUSE,
                    operator=SYSTEM,
                    reason="stops cegos com posicao aberta por tempo prolongado",
                ),
                gateway_state=self._blob(),
                audit={"positions": positions},
            )

    async def _reconcile(self, now: datetime, *, startup: bool = False) -> list[Divergence]:
        if self.journal.down:
            if not self._reconcile_suspended:
                self._reconcile_suspended = True
                log.error("reconcile_suspended", reason="banco indisponivel")
                await self._alert("reconcile_suspended", "reconciliacao suspensa: banco indisponivel")
            return []
        journal_positions = await self.journal.open_positions()
        if self.journal.down:
            return []
        pending_partial = {
            live.symbol: live.partial_fill.quantity
            for live in self._live.values()
            if live.partial_fill is not None
        }
        local = [
            p.model_copy(update={"quantity": p.quantity - pending_partial.get(p.symbol, Decimal(0))})
            if p.symbol in pending_partial
            else p
            for p in journal_positions
        ]
        balances = dict(await self.gateway.get_balances())
        for live in self._live.values():
            if live.emergency is not None:
                asset = live.symbol.split("/")[0]
                held = balances[asset]
                balances[asset] = held.model_copy(
                    update={"free": max(held.free - live.emergency.quantity, Decimal(0))}
                )
        stops = tuple(
            ProtectiveStop(symbol=o.symbol, quantity=o.quantity, stop_price=o.stop)
            for o in self.gateway.open_protective_state()
        )
        snapshot = AccountSnapshot(balances=balances, stops=stops, prices=self._prices())
        found = list(reconcile(local, snapshot, config=self.risk))
        found.extend(self._journal_divergences(journal_positions))
        if found:
            details = "; ".join(d.detail for d in found)
            log.error("reconcile_divergence", startup=startup, details=details)
            if await self._pause(f"reconciliacao: {details}"):
                await self._alert("reconcile_divergence", details)
                await self._publish_state("reconcile")
        else:
            log.info("reconcile_ok", startup=startup, positions=len(journal_positions))
        return found

    def _journal_divergences(self, journal_positions: list[ManagedPosition]) -> list[Divergence]:
        persisted = {p.symbol: p for p in journal_positions}
        skipped = {
            lv.symbol for lv in self._live.values() if lv.partial_fill is not None or lv.position is None
        }
        memory = {lv.symbol: lv.position for lv in self._live.values() if lv.position is not None}
        found: list[Divergence] = []
        for symbol in sorted((memory.keys() | persisted.keys()) - skipped):
            mem, stored = memory.get(symbol), persisted.get(symbol)
            if mem is None or stored is None or (mem.quantity, mem.stop) != (stored.quantity, stored.stop):
                found.append(
                    Divergence(
                        kind=DivergenceKind.UNTRACKED_BALANCE,
                        subject=symbol,
                        expected=stored.quantity if stored else Decimal(0),
                        actual=mem.quantity if mem else Decimal(0),
                        detail=f"{symbol}: posicao em memoria x diario divergem",
                    )
                )
        return found

    def _prices(self) -> dict[str, Decimal]:
        return {s.split("/")[0]: p for s, p in self._marks.items()}

    # ------------------------------------------------------------------ comandos do operador

    async def _consume_commands(self, now: datetime) -> None:
        queue = self.commands
        if queue is None:
            return
        await self._pull_signed(now)
        waiting = queue.signed_names()
        for path, command in queue.pending(now, not_before=self.started_at):
            try:
                async with self._lock:
                    result = await self._execute(command, now)
            except Exception as error:
                if not await self._command_failed(path, command, error):
                    raise
                waiting.discard(path.name)
                await self._send_ack(queue.signed_id(path.name), "failed", "falhou ao executar")
                continue
            if result.status is not CommandStatus.APPLIED:
                log.warning("operator_command_result", status=result.status.value, detail=result.detail)
            queue.done(path, result)
            waiting.discard(path.name)
            await self._send_ack(queue.signed_id(path.name), result.status.value, result.detail)
        for name in sorted(waiting):
            if not queue.exists(name):
                # saiu da fila sem executar (expirou, foi rejeitado ou falhou 3 vezes): confirma o desfecho
                state, detail = queue.outcome_of(name) or ("expired", "")
                await self._send_ack(queue.signed_id(name), state, detail)

    async def _pull_signed(self, now: datetime) -> None:
        """Comandos assinados da API (Redis): so os verificados entram na fila local, onde valem TODAS as
        protecoes de sempre (TTL, descarte pre-partida, resume nunca reativa HALTED, tentativas). Cada
        envelope e tratado isoladamente: um veneno nunca faz perder os comandos legitimos do mesmo lote."""
        source = self.signed_commands
        if source is None or self.commands is None:
            return
        try:
            outcomes = await source.poll(now)
            self._signed_blind = int(getattr(source, "poll_failures", 0))
        except Exception as error:
            self._signed_blind += 1
            log.error("signed_commands_failed", error=scrub_text(repr(error)))
            outcomes = []
        if self._signed_blind:
            log.error("signed_commands_blind", cycles=self._signed_blind)
            if self._signed_blind == _SIGNED_BLIND_ALERT_CYCLES:
                await self._alert(
                    "command_channel_blind",
                    f"o canal de comandos falha ha {self._signed_blind} ciclos: Parar tudo pode nao chegar",
                )
        for outcome in outcomes:
            try:
                await self._intake(source, outcome, now)
            except Exception as error:
                # o envelope segue em `processing` e volta no proximo poll (at-least-once)
                log.error("signed_command_intake_failed", error=scrub_text(repr(error)))
        if (burst := self._discards.flush(now)) is not None:
            log.error("paper_command_discard_burst", counts=burst)
            await self._audit("paper_command_discard_burst", {"counts": burst, "total": sum(burst.values())})

    async def _intake(self, source: SignedCommandSource, outcome: Outcome, now: datetime) -> None:
        queue = self.commands
        if queue is None:
            return
        if outcome.command is None:
            code = outcome.discarded or "desconhecido"
            if self._discards.admit(code, now):
                log.error(
                    "paper_command_discarded", command_id=outcome.command_id, code=code, digest=outcome.digest
                )
                await self._audit(
                    "paper_command_discarded",
                    {"command_id": outcome.command_id, "code": code, "digest": outcome.digest},
                )
            if outcome.command_id and outcome.authentic:
                await source.ack(outcome.command_id, "discarded", code)
            await source.release(outcome)
            return
        command_id = outcome.command_id
        path = queue.path_for(outcome.command, command_id)
        existed = path.exists() or queue.archived_in(path.name) is not None
        if not existed:
            queue.send(outcome.command, command_id)
        if not await source.commit(outcome):
            if not existed:
                path.unlink(missing_ok=True)
            if self._discards.admit("replay", now):
                await self._audit(
                    "paper_command_discarded",
                    {"command_id": command_id, "code": "replay", "digest": outcome.digest},
                )
            return
        if command_id:
            await source.ack(command_id, "received")

    async def _send_ack(self, command_id: str | None, result: str, detail: str = "") -> None:
        if self.signed_commands is None or command_id is None:
            return
        try:
            await self.signed_commands.ack(command_id, result, detail)
        except Exception as error:
            log.warning("signed_command_ack_failed", error=scrub_text(repr(error)))

    async def _audit(self, action: str, payload: Mapping[str, Any]) -> None:
        if self.audit_sink is None:
            return
        try:
            await self.audit_sink.audit(SYSTEM, action, self.run_id, payload)
        except Exception as error:
            log.error("audit_sink_failed", action=action, error=scrub_text(repr(error)))

    async def _command_failed(self, path: Path, command: Command, error: Exception) -> bool:
        """Comando que falhou NAO e arquivado como processado: segue pendente, com alerta. Erro de
        infraestrutura nao conta tentativa; falha deterministica e contada no disco e, na 3a, o comando vai
        para `failed/` (devolve True: a partida/o laco seguem sem cair)."""
        if is_transient(error) or self.commands is None:
            return False
        attempts = self.commands.register_failure(path)
        log.error(
            "operator_command_failed",
            command=command.name.value,
            attempt=attempts,
            error=scrub_text(repr(error)),
        )
        await self._alert("command_failed", f"{command.name.value} falhou (tentativa {attempts})")
        if attempts < _MAX_COMMAND_ATTEMPTS:
            return False
        self.commands.fail(path)
        await self._alert(
            "command_rejected", f"{command.name.value} movido para failed/ apos {attempts} falhas"
        )
        return True

    async def _execute(self, command: Command, now: datetime) -> CommandResult:
        """Executa e DEVOLVE o resultado real: recusa nunca vira 'aplicado' (o painel e o `ack` refletem)."""
        reason = scrub_text(command.reason)
        audit = {
            "command_sha256": command.digest(),
            "command": command.name.value,
            "liquidate": command.liquidate,
        }
        log.warning("operator_command", command=command.name.value, operator=command.operator, reason=reason)
        match command.name:
            case CommandName.PAUSE:
                fresh = await self._pause(reason, operator=command.operator, audit=audit)
                return CommandResult.applied("" if fresh else "o robô já estava pausado por este motivo")
            case CommandName.RESUME:
                if self.state.halted:
                    log.error("resume_refused", halted=True, paused=self.state.paused)
                    return CommandResult.refused(
                        "o robô está desligado por drawdown; só a reativação manual o religa"
                    )
                if not self.state.paused:
                    log.error("resume_refused", halted=False, paused=False)
                    return CommandResult.refused("o robô não estava pausado")
                self.state = self.state.resume(operator=command.operator, reason=reason, now=now)
                self._pauses.clear()
                await self.journal.record_control(
                    ControlEvent(
                        at=now, action=ControlAction.RESUME, operator=command.operator, reason=reason
                    ),
                    risk_state=self.state,
                    gateway_state=self._blob(),
                    audit=audit,
                )
                await self._publish_state("resume")
            case CommandName.REACTIVATE:
                if not self.state.halted:
                    log.error("reactivate_refused", reason="o robo nao esta desligado por drawdown")
                    return CommandResult.refused("o robô não está desligado por drawdown")
                self.state = self.state.reactivate(operator=command.operator, reason=reason, now=now)
                await self.journal.record_control(
                    ControlEvent(
                        at=now, action=ControlAction.REACTIVATE, operator=command.operator, reason=reason
                    ),
                    risk_state=self.state,
                    gateway_state=self._blob(),
                    audit=audit,
                )
                await self._publish_state("reactivate")
            case CommandName.STOP:
                self.request_stop(f"{command.operator}: {reason}")
            case CommandName.EMERGENCY_STOP:
                await self._emergency_stop(command, reason, now, audit)
            case CommandName.APPLY_CONFIG:
                return await self._apply_config(command, audit)
        return CommandResult.applied()

    async def _apply_config(self, command: Command, audit: dict[str, Any]) -> CommandResult:
        """Aplica ao robo em execucao SO reducoes de risco, CAMPO A CAMPO: o que reduz (ou e igual) vale, o
        que aumenta e recusado e fica para o proximo inicio. Campo desconhecido ou valor invalido recusa o
        pacote todo (comando mal formado). O servidor nunca confia no remetente para relaxar limites."""
        patch: Any = command.params.get("risk")
        current = self.risk.model_dump(mode="json")
        if not isinstance(patch, dict) or not patch:
            return await self._config_refused(command, audit, "sem campos de risco")
        if unknown := sorted(set(patch) - set(current)):
            return await self._config_refused(command, audit, f"campos desconhecidos {unknown}")
        try:
            fresh = RiskConfig.model_validate({**current, **patch}).model_dump(mode="json")
        except ValidationError as error:
            detail = f"valores invalidos: {scrub_text(str(error.errors(include_input=False))[:200])}"
            return await self._config_refused(command, audit, detail)
        safe = (Effect.DECREASE, Effect.NEUTRAL)
        kept = {n: patch[n] for n in patch if risk_effect(n, current[n], fresh[n]) in safe}
        raised = sorted(set(patch) - set(kept))
        try:
            wanted = RiskConfig.model_validate({**current, **kept})
        except ValidationError:
            return await self._config_refused(
                command, audit, "as reducoes isoladas deixariam a configuracao invalida"
            )
        changed = sorted(n for n in kept if current[n] != wanted.model_dump(mode="json")[n])
        if raised:
            reason = f"aumenta o risco, so vale no proximo inicio: {raised}"
            await self._audit(
                "config_apply_refused", {**audit, "operator": command.operator, "reason": reason}
            )
            await self._alert("config_refused", reason)
            if not changed:
                log.error("apply_config_refused", reason=reason)
                return CommandResult.refused(reason)
        if changed:
            self.risk = wanted
            self.config = self.config.model_copy(update={"risk": wanted})
            self.context = StrategyContext(taker_fee_rate=wanted.taker_fee_rate)
            log.warning("config_applied", fields=changed, operator=command.operator)
            await self._audit(
                "config_applied",
                {
                    **audit,
                    "operator": command.operator,
                    "fields": changed,
                    "config_hash": self.config.config_hash(),
                },
            )
        if raised:
            await self._publish_state("config_applied")
            return CommandResult.partial(
                f"aplicado: {changed}; recusado (aumenta o risco, so no proximo inicio): {raised}"
            )
        version = command.params.get("version")
        if isinstance(version, int) and not isinstance(version, bool):
            self._applied_version = version
        if changed:
            await self._publish_state("config_applied")
        return CommandResult.applied("" if changed else "nada mudou: os valores ja estavam em vigor")

    async def _config_refused(self, command: Command, audit: dict[str, Any], reason: str) -> CommandResult:
        log.error("apply_config_refused", reason=reason)
        await self._audit("config_apply_refused", {**audit, "operator": command.operator, "reason": reason})
        await self._alert("config_refused", reason)
        return CommandResult.refused(reason)

    async def _emergency_stop(
        self, command: Command, reason: str, now: datetime, audit: dict[str, Any]
    ) -> None:
        """Bloqueia entradas na hora; opcionalmente liquida a mercado (saida MANUAL), nunca remove o stop."""
        await self._pause(f"emergency-stop: {reason}", operator=command.operator, audit=audit)
        for pending in list(self._pending.values()):
            await self._cancel_entry(
                pending, "emergency_stop", "parada de emergencia: ordem de entrada cancelada", now
            )
        await self._alert("emergency_stop", reason)
        if not (command.liquidate or self._emergency_liquidate):
            return
        await self._process_fills()
        for live in list(self._live.values()):
            if live.position is None or live.closing is not None:
                continue
            if live.partial_fill is not None:
                await self._apply_partial(live, live.partial_fill)
            position = live.position
            live.closing = ExitFill(
                fill_id=exit_fill_id(position, now, ExitReason.MANUAL),
                quantity=position.quantity,
                price=self._marks.get(live.symbol, position.entry_price),
                reason=ExitReason.MANUAL,
                at=now,
            )
            await self._cancel_partial(live)
        await self._place_exits()

    # ------------------------------------------------------------------ recuperacao

    def _restore_lives(self, runner: RunnerSnapshot, positions: Sequence[ManagedPosition]) -> None:
        by_symbol = {p.symbol: p for p in positions}
        for snap in runner.lives:
            position = by_symbol.get(snap.symbol)
            if position is None and snap.emergency is None:
                log.error("live_without_position", symbol=snap.symbol)
                self._restore_issues.append(f"{snap.symbol}: estado do robo sem posicao no diario")
                continue
            self._live[snap.entry_id] = _Live(
                entry_id=snap.entry_id,
                symbol=snap.symbol,
                strategy=snap.strategy,
                risk_amount=snap.risk_amount,
                gateway_stop=snap.gateway_stop,
                entry_order=snap.entry_order,
                position=position,
                emergency=snap.emergency,
                partial_order=snap.partial_order,
                partial_fill=snap.partial_fill,
                closing=snap.closing,
                exit_order=snap.exit_order,
                exit_seq=snap.exit_seq,
            )
        orphans = set(by_symbol) - {lv.symbol for lv in self._live.values()}
        if orphans:
            log.error("positions_without_runner_state", symbols=sorted(orphans))
            self._restore_issues.append(f"posicoes no diario sem estado do robo: {sorted(orphans)}")

    async def _flag_restore_issues(self) -> None:
        """Inconsistencia na recuperacao: alerta e pausa (a reconciliacao da partida confere o gateway)."""
        if not self._restore_issues:
            return
        details = "; ".join(self._restore_issues)
        self._restore_issues = []
        await self._alert("restore_inconsistent", details)
        await self._pause(f"recuperacao inconsistente: {details}")

    async def _cancel_orphans(self, open_orders: Sequence[OrderRecord], now: datetime) -> None:
        """Ordens abertas no diario que o gateway restaurado nao conhece (queda entre colocar e gravar)."""
        snapshot = self.gateway.snapshot(compact=True)
        known = {o.order.client_order_id for o in snapshot.orders if o.status is OrderStatus.NEW}
        known |= {
            f"{o.entry_client_order_id}:{OCO_LEG}" for o in snapshot.ocos if o.status is OrderStatus.NEW
        }
        known |= {f.client_order_id for f in self.gateway.fills if f.sequence > self._seen_seq}
        for order in open_orders:
            if order.client_order_id not in known:
                log.warning("orphan_order_closed", order=order.client_order_id)
                self._skip_used_exit_id(order.client_order_id)
                await self.journal.update_order(
                    order.model_copy(
                        update={"status": OrderStatus.CANCELED, "updated_at": max(now, order.created_at)}
                    )
                )

    async def _cancel_pending_after_restart(self, now: datetime) -> None:
        """A aprovacao do gate nao sobrevive ao processo: entradas pendentes sao canceladas (sinal velho)."""
        snapshot = self.gateway.snapshot(compact=True)
        for state in snapshot.orders:
            order = state.order
            if state.status is not OrderStatus.NEW or not isinstance(order, EntryOrder):
                continue
            report = await self._cancel_order(order.symbol, order.client_order_id)
            if report is None or report.status is not OrderStatus.CANCELED:
                continue
            record = OrderRecord.from_order(order, status=OrderStatus.CANCELED, at=max(now, order.created_at))
            await self._record(
                self._decision(
                    at=now,
                    symbol=order.symbol,
                    action=DecisionAction.SKIP,
                    traded=False,
                    code="entry_cancelled_on_restart",
                    reason="ordem de entrada pendente cancelada na recuperacao apos queda",
                    order_id=order.client_order_id,
                ),
                order=record,
                with_state=True,
            )

    def _skip_used_exit_id(self, client_order_id: str) -> None:
        """Id de saida ja gravado no diario (mesmo orfao) nunca e reusado: o `exit_seq` passa dele."""
        entry_id, _, leg = client_order_id.partition(":")
        live = self._live.get(entry_id)
        digits = leg.removeprefix(EMERGENCY_LEG).removeprefix(EXIT_LEG)
        if live is not None and digits.isdigit() and leg.startswith((EXIT_LEG, EMERGENCY_LEG)):
            live.exit_seq = max(live.exit_seq, int(digits))

    # ------------------------------------------------------------------ estado, eventos, status

    def _mark_seen(self, fill: Fill) -> None:
        self._seen_seq = max(self._seen_seq, fill.sequence)

    def _runner_snapshot(self, seen_seq: int) -> RunnerSnapshot:
        return RunnerSnapshot(
            lives=tuple(
                LiveSnapshot(
                    entry_id=lv.entry_id,
                    symbol=lv.symbol,
                    strategy=lv.strategy,
                    risk_amount=lv.risk_amount,
                    gateway_stop=lv.gateway_stop,
                    entry_order=lv.entry_order,
                    partial_order=lv.partial_order,
                    partial_fill=lv.partial_fill,
                    closing=lv.closing,
                    exit_order=lv.exit_order,
                    exit_seq=lv.exit_seq,
                    emergency=lv.emergency,
                )
                for lv in self._live.values()
            ),
            handled=dict(self._last_handled),
            seen_seq=seen_seq,
            pauses=tuple(self._pauses.values()),
        )

    def _blob(self, fill_sequence: int = 0) -> dict[str, Any]:
        """`seen_seq` do blob = o ultimo fill cujos efeitos ja estao no diario OU vao na mesma transacao:
        o `fill_seq` do gateway pode estar adiantado (stop da mesma vela) e nao serve de cursor."""
        seen = max(self._seen_seq, fill_sequence)
        return encode_blob(self.gateway.snapshot(compact=True), self._runner_snapshot(seen))

    def _settled_blob(self) -> dict[str, Any] | None:
        """Estado do gateway so se todo fill ja foi tratado e gravado: gravado junto de um evento que nao e o
        fill, ele carregaria efeitos (ex.: o stop executado) que o diario ainda nao tem e a recuperacao os
        daria por processados."""
        fills = self.gateway.fills
        if fills and fills[-1].sequence > self._seen_seq:
            return None
        return self._blob()

    async def _save_state(self, now: datetime) -> None:
        await self.journal.save_state(self.state, self._blob(), now)

    def _config_payload(self) -> dict[str, Any]:
        return {
            "config": self.config.model_dump(mode="json"),
            "config_hash": self.config.config_hash(),
            "allow_open_calendar": self.config.allow_open_calendar,
            "config_source": self.config_source,
        }

    def _exposure(self) -> tuple[Decimal, dict[str, Decimal]]:
        """Fracao do patrimonio comprada (total e por moeda), ao ultimo preco conhecido."""
        equity = self._mtm_equity()
        by_coin: dict[str, Decimal] = {}
        for live in self._live.values():
            if live.position is None or equity <= 0:
                continue
            price = self._marks.get(live.symbol) or live.position.entry_price
            coin = live.symbol.split("/")[0]
            by_coin[coin] = by_coin.get(coin, Decimal(0)) + live.position.quantity * price / equity
        return sum(by_coin.values(), Decimal(0)), by_coin

    def status_payload(self, now: datetime) -> dict[str, Any]:
        state = self.state
        exposure, by_coin = self._exposure()
        return {
            "run_id": self.run_id,
            "state": self.bot_state.value,
            "heartbeat_at": now.isoformat(),
            "started_at": self.started_at.isoformat(),
            "code_version": self.code_version,
            "stops_blind": self._blind,
            "halted": state.halted,
            "halt_reason": state.halt_reason,
            "paused": state.paused,
            "pause_reason": state.pause_reason,
            "pause_reasons": [e.model_dump(mode="json") for e in self._pauses.values()],
            "equity": str(self._mtm_equity()),
            "realized_equity": str(state.equity),
            "peak_equity": str(state.peak_equity),
            "drawdown": str(state.drawdown),
            "day": state.day.isoformat(),
            "day_pnl": str(state.day_pnl),
            "day_start_equity": str(state.day_start_equity),
            "loss_streaks": dict(state.loss_streaks),
            "exposure": str(exposure),
            "coin_exposure": {c: str(v) for c, v in by_coin.items()},
            "daily_pause_until": state.daily_pause_until.isoformat() if state.daily_pause_until else None,
            "target_reached_on": state.target_reached_on.isoformat() if state.target_reached_on else None,
            "pair_pause_until": {s: t.isoformat() for s, t in state.pair_pause_until.items()},
            "open_positions": self.live_symbols,
            "pending_entries": sorted(self._pending),
            "last_candle": {s: t.isoformat() for s, t in self._last_handled.items()},
            "redis_healthy": self.bus.healthy,
            "allow_open_calendar": self.config.allow_open_calendar,
            "config_hash": self.config.config_hash(),
            "config_version": None if self.config_source is None else self.config_source.get("version_id"),
            "applied_config_version": self._applied_version,
            "risk": self.risk.model_dump(mode="json"),
            "outbox_pending": self.journal.pending_events,
            "persistence_error": self.journal.last_error if self.journal.down else None,
            "ai": self._ai_payload(),
        }

    def _ai_payload(self) -> dict[str, Any]:
        detail = getattr(self.analyst, "status", None)
        extra = detail() if callable(detail) else {}
        return {"status": self._ai_status, "enabled": self.analyst is not None, **extra}

    def _write_status(self, now: datetime) -> None:
        if self.paths is not None:
            write_status(self.paths.status, self.status_payload(now))

    async def _publish_state(self, event: str) -> None:
        payload = {"event": event, **self.status_payload(self.clock.now())}
        self._write_status(self.clock.now())
        await self._emit("state", payload)

    async def _persistence_down(self, error: str) -> None:
        """Banco fora apos todas as tentativas: eventos seguem no outbox, entradas bloqueadas, alerta alto."""
        reason = scrub_text(error)
        log.critical("FAILED_PERSISTENCE", error=reason, queued=self.journal.pending_events)
        await self._alert(
            "persistence_down",
            f"banco indisponivel; {self.journal.pending_events} evento(s) no outbox: {reason}",
        )
        await self._pause(f"{PERSISTENCE_PAUSE}{reason}")
        await self._publish_state("persistence_down")

    async def _persistence_up(self) -> None:
        """Outbox reaplicado: reconcilia ANTES de retomar e retira so o motivo 'banco fora'; o robo volta a
        operar apenas se nenhum outro motivo (operador, divergencia, ...) seguir ativo."""
        now = self.clock.now()
        await self._alert("persistence_recovered", "banco disponivel: outbox reaplicado")
        if self._reconcile_suspended:
            self._reconcile_suspended = False
            await self._reconcile(now)
        for reason in [r for r in self._pauses if r.startswith(PERSISTENCE_PAUSE)]:
            del self._pauses[reason]
        if self.state.paused and not self._pauses:
            self.state = self.state.resume(
                operator=SYSTEM, reason="banco disponivel: outbox reaplicado", now=now
            )
            await self.journal.record_control(
                ControlEvent(
                    at=now,
                    action=ControlAction.RESUME,
                    operator=SYSTEM,
                    reason="banco disponivel: outbox reaplicado",
                ),
                risk_state=self.state,
                gateway_state=self._blob(),
            )
        elif self._pauses:
            remaining = next(iter(self._pauses))
            self.state = self.state.model_copy(update={"pause_reason": remaining})
            log.warning("persistence_recovered_still_paused", reasons=list(self._pauses))
        await self._save_state(now)
        await self._publish_state("persistence_recovered")

    async def _alert(self, kind: str, message: str) -> None:
        message = scrub_text(message)
        log.error("ALERT", kind=kind, message=message)
        await self._emit("alerts", {"alert": kind, "message": message})
        self._notify(kind, message)

    def _notify(self, kind: str, message: str) -> None:
        """Canal externo (Telegram/e-mail): agenda e volta; falha de notificacao nunca afeta o robo."""
        if self.alerts is None:
            return
        try:
            self.alerts.dispatch(kind, message)
        except Exception as error:
            log.warning("notification_dispatch_failed", error=type(error).__name__)

    async def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        """Eventos sao best-effort: nenhuma falha do barramento pode parar o robo."""
        try:
            await self.bus.publish(
                kind, {"run_id": self.run_id, "at": self.clock.now().isoformat(), "kind": kind, **payload}
            )
        except Exception as error:
            log.error("event_bus_failed", kind=kind, error=scrub_text(repr(error)))


def _ai_reason(code: str, opinion: AnalystOpinion, confidence: float, config: PaperRunConfig) -> str:
    if code == AI_LOW_CONFIDENCE:
        return (
            f"IA reduziu a confianca para {confidence:.0%}, abaixo do minimo de "
            f"{config.ai_min_confidence:.0%}: {opinion.reason}"
        )
    return f"IA vetou a entrada: {opinion.reason}"


def _entry_id(candle: Candle, index: int = 0) -> str:
    """Um id por sinal: o 1o mantem `e<aammddhhmm><ativo>`; os demais da mesma vela ganham `-<n>`."""
    base = candle.symbol.split("/")[0]
    return f"e{candle.close_time:%y%m%d%H%M}{base}" + (f"-{index}" if index else "")


def _sequence(fill: Fill) -> int:
    return fill.sequence


def _floor(moment: datetime, seconds: int) -> datetime:
    return datetime.fromtimestamp(int(moment.timestamp()) // seconds * seconds, tz=moment.tzinfo)


def _open_time(candle: Candle) -> datetime:
    return candle.open_time


def _fill_record(fill: Fill, quote: str) -> FillRecord:
    return FillRecord(
        fill_id=f"{fill.client_order_id}#{fill.sequence}",
        client_order_id=fill.client_order_id,
        symbol=fill.symbol,
        side=fill.side,
        quantity=fill.quantity,
        price=fill.price,
        fee=fill.fee,
        fee_asset=quote,
        filled_at=fill.at,
    )


def _filled(record: OrderRecord, fill: Fill) -> OrderRecord:
    return record.model_copy(
        update={
            "status": OrderStatus.FILLED,
            "filled_quantity": fill.quantity,
            "average_price": fill.price,
            "updated_at": max(fill.at, record.created_at),
        }
    )


def _fill_payload(event: str, fill: Fill, entry_id: str) -> dict[str, Any]:
    return {"event": event, "entry_id": entry_id, "fill": fill.model_dump(mode="json")}


def _decision_payload(entry: DecisionEntry) -> dict[str, Any]:
    return {"decision": entry.model_dump(mode="json")}


async def _aclose(iterator: Any) -> None:
    if (close := getattr(iterator, "aclose", None)) is not None:
        with contextlib.suppress(Exception):
            await close()
