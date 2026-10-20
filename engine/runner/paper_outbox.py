"""Gravacao durável do diario: retry idempotente, fila local (outbox) e modo FAILED_PERSISTENCE.

`DurableJournal` envolve o diario real. Queda transitoria do banco (OSError, TimeoutError, erros de conexao do
driver) e retentada com backoff; as gravacoes sao idempotentes, entao repetir e seguro. Se a queda persiste, o
evento NUNCA e descartado: vai para o outbox (um arquivo por evento em `<run>/outbox`, fsync + rename atomico)
e o diario entra em modo `down`. Nesse modo todo evento novo e enfileirado em ordem e so o `flush` (tentado
pelo robo a cada ciclo e na partida) reaplica o outbox no banco. O estado periodico (`save_state`) e a curva
de equity sao reconstruiveis e simplesmente deixam de ser gravados enquanto o banco esta fora.
"""

import asyncio
import os
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Final, Literal

import structlog
from pydantic import Field, TypeAdapter
from sqlalchemy.exc import DataError, DBAPIError, IntegrityError, NotSupportedError, ProgrammingError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from engine.application.backtest.models import EquityPoint
from engine.application.risk import ManagedPosition, RiskState
from engine.domain.base import DomainModel
from engine.domain.journal import ControlEvent, DecisionEntry, FillRecord, OrderRecord, RunRecord
from engine.domain.trade import Trade
from engine.infrastructure.config.logging import scrub_text
from engine.infrastructure.persistence.recovery import RecoveryState
from engine.runner.paper_journal import Journal

log = structlog.get_logger()

DEFAULT_RETRY_DELAYS: Final = (0.05, 0.5, 2.0, 5.0)
_DETERMINISTIC = (IntegrityError, DataError, ProgrammingError, NotSupportedError)


class PersistenceUnavailableError(RuntimeError):
    """O banco continuou indisponivel depois de todas as tentativas."""


def is_transient(error: BaseException) -> bool:
    """Falha de infraestrutura (vale repetir): rede, timeout, pool e erros de conexao do driver."""
    if isinstance(error, OSError | TimeoutError | PoolTimeoutError):
        return True
    return isinstance(error, DBAPIError) and not isinstance(error, _DETERMINISTIC)


class _DecisionCall(DomainModel):
    method: Literal["record_decision"] = "record_decision"
    entry: DecisionEntry
    order: OrderRecord | None = None
    gateway_state: dict[str, Any] | None = None

    async def apply(self, journal: Journal) -> None:
        await journal.record_decision(self.entry, order=self.order, gateway_state=self.gateway_state)


class _OrderCall(DomainModel):
    method: Literal["update_order"] = "update_order"
    order: OrderRecord
    gateway_state: dict[str, Any] | None = None

    async def apply(self, journal: Journal) -> None:
        await journal.update_order(self.order, gateway_state=self.gateway_state)


class _EntryCall(DomainModel):
    method: Literal["record_entry"] = "record_entry"
    orders: tuple[OrderRecord, ...]
    fill: FillRecord
    position: ManagedPosition | None
    risk_state: RiskState
    gateway_state: dict[str, Any]
    decision: DecisionEntry | None = None

    async def apply(self, journal: Journal) -> None:
        await journal.record_entry(
            orders=self.orders,
            fill=self.fill,
            position=self.position,
            risk_state=self.risk_state,
            gateway_state=self.gateway_state,
            decision=self.decision,
        )


class _ExitCall(DomainModel):
    method: Literal["record_exit"] = "record_exit"
    orders: tuple[OrderRecord, ...]
    fill: FillRecord
    trade: Trade
    position: ManagedPosition | None
    risk_state: RiskState
    strategy: str | None
    risk_amount: Decimal | None
    gateway_state: dict[str, Any]
    without_position_row: bool = False
    halt: ControlEvent | None = None

    async def apply(self, journal: Journal) -> None:
        await journal.record_exit(
            orders=self.orders,
            fill=self.fill,
            trade=self.trade,
            position=self.position,
            risk_state=self.risk_state,
            strategy=self.strategy,
            risk_amount=self.risk_amount,
            gateway_state=self.gateway_state,
            without_position_row=self.without_position_row,
            halt=self.halt,
        )


class _PositionCall(DomainModel):
    method: Literal["record_position"] = "record_position"
    position: ManagedPosition
    gateway_state: dict[str, Any]

    async def apply(self, journal: Journal) -> None:
        await journal.record_position(self.position, self.gateway_state)


class _ControlCall(DomainModel):
    method: Literal["record_control"] = "record_control"
    event: ControlEvent
    risk_state: RiskState | None = None
    gateway_state: dict[str, Any] | None = None
    audit: dict[str, Any] | None = None

    async def apply(self, journal: Journal) -> None:
        await journal.record_control(
            self.event, risk_state=self.risk_state, gateway_state=self.gateway_state, audit=self.audit
        )


type JournalCall = _DecisionCall | _OrderCall | _EntryCall | _ExitCall | _PositionCall | _ControlCall
_CALLS: TypeAdapter[JournalCall] = TypeAdapter(Annotated[JournalCall, Field(discriminator="method")])


class Outbox:
    """Fila FIFO de chamadas pendentes: em arquivos (sobrevive ao processo) ou, sem diretorio, em memoria."""

    def __init__(self, directory: Path | None) -> None:
        self._dir = directory
        self._memory: dict[str, str] = {}
        self._counter = self._highest_counter_on_disk()
        self._last_ns = 0

    def _highest_counter_on_disk(self) -> int:
        if self._dir is None or not self._dir.is_dir():
            return 0
        return max((int(path.stem.rpartition("-")[2]) for path in self._dir.glob("*-*.json")), default=0)

    def append(self, call: JournalCall) -> None:
        self._last_ns = max(time.time_ns(), self._last_ns)
        key = f"{self._last_ns:020d}-{self._next():012d}"
        body = _CALLS.dump_json(call).decode()
        if self._dir is None:
            self._memory[key] = body
            return
        self._dir.mkdir(parents=True, exist_ok=True)
        temp = self._dir / f"{key}.tmp"
        with temp.open("w", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, self._dir / f"{key}.json")

    def pending(self) -> list[tuple[str, JournalCall]]:
        if self._dir is None:
            return [(key, _CALLS.validate_json(body)) for key, body in sorted(self._memory.items())]
        if not self._dir.is_dir():
            return []
        return [
            (path.stem, _CALLS.validate_json(path.read_text(encoding="utf-8")))
            for path in sorted(self._dir.glob("*.json"))
        ]

    def __len__(self) -> int:
        if self._dir is None:
            return len(self._memory)
        return sum(1 for _ in self._dir.glob("*.json")) if self._dir.is_dir() else 0

    def done(self, key: str) -> None:
        if self._dir is None:
            self._memory.pop(key, None)
            return
        applied = self._dir / "applied"
        applied.mkdir(exist_ok=True)
        os.replace(self._dir / f"{key}.json", applied / f"{key}.json")

    def quarantine(self, key: str) -> None:
        if self._dir is None:
            self._memory.pop(key, None)
            return
        failed = self._dir / "failed"
        failed.mkdir(exist_ok=True)
        os.replace(self._dir / f"{key}.json", failed / f"{key}.json")

    def _next(self) -> int:
        self._counter += 1
        return self._counter


class DurableJournal:
    """Mesma interface do `Journal`, com retry, outbox e modo `down`. O robo so ve este objeto."""

    def __init__(
        self,
        inner: Journal,
        outbox: Outbox | None = None,
        retry_delays: Sequence[float] = DEFAULT_RETRY_DELAYS,
    ) -> None:
        self._inner = inner
        self.outbox = Outbox(None) if outbox is None else outbox
        self._delays = tuple(retry_delays)
        self.down = False
        self.last_error: str | None = None
        self.on_down: Callable[[str], Awaitable[None]] | None = None
        self.on_up: Callable[[], Awaitable[None]] | None = None

    @property
    def run_id(self) -> str:
        return self._inner.run_id

    @property
    def pending_events(self) -> int:
        return len(self.outbox)

    # ------------------------------------------------------------------ nucleo

    async def _retry[T](self, operation: Callable[[], Awaitable[T]]) -> T:
        delays = iter(self._delays)
        while True:
            try:
                return await operation()
            except Exception as error:
                if not is_transient(error):
                    raise
                delay = next(delays, None)
                if delay is None:
                    raise PersistenceUnavailableError(scrub_text(repr(error))) from error
                log.error("journal_retry", error=scrub_text(repr(error)), retry_in=delay)
                await asyncio.sleep(delay)

    async def _go_down(self, error: BaseException) -> None:
        self.last_error = scrub_text(repr(error))
        if self.down:
            return
        self.down = True
        log.critical("PERSISTENCE_DOWN", error=self.last_error, queued=len(self.outbox))
        if self.on_down is not None:
            await self.on_down(self.last_error)

    async def _write(self, call: JournalCall) -> None:
        if self.down or len(self.outbox):
            self.outbox.append(call)
            return
        try:
            await self._retry(lambda: call.apply(self._inner))
        except PersistenceUnavailableError as error:
            self.outbox.append(call)
            await self._go_down(error)

    async def flush(self) -> bool:
        """Reaplica o outbox em ordem; False se o banco ainda esta fora (o resto fica para depois)."""
        for key, call in self.outbox.pending():
            try:
                await call.apply(self._inner)
            except Exception as error:
                if is_transient(error):
                    self.last_error = scrub_text(repr(error))
                    return False
                self.outbox.quarantine(key)
                log.critical(
                    "outbox_event_rejected", key=key, method=call.method, error=scrub_text(repr(error))
                )
                raise
            self.outbox.done(key)
        if self.down:
            self.down = False
            log.warning("PERSISTENCE_RECOVERED")
            if self.on_up is not None:
                await self.on_up()
        return True

    # ------------------------------------------------------------------ leituras

    async def load(self) -> RecoveryState:
        return await self._retry(self._inner.load)

    async def previous_halt(self) -> tuple[str, str] | None:
        return await self._retry(self._inner.previous_halt)

    async def has_decision(self, symbol: str, decided_at: datetime) -> bool:
        return await self._read(lambda: self._inner.has_decision(symbol, decided_at), default=False)

    async def open_positions(self) -> list[ManagedPosition]:
        return await self._read(self._inner.open_positions, default=[])

    async def _read[T](self, operation: Callable[[], Awaitable[T]], *, default: T) -> T:
        if self.down:
            return default
        try:
            return await self._retry(operation)
        except PersistenceUnavailableError as error:
            await self._go_down(error)
            return default

    # ------------------------------------------------------------------ gravacoes de eventos

    async def start_run(self, run: RunRecord, config: Mapping[str, Any], event: ControlEvent) -> None:
        await self._retry(lambda: self._inner.start_run(run, config, event))

    async def record_decision(
        self,
        entry: DecisionEntry,
        *,
        order: OrderRecord | None = None,
        gateway_state: Mapping[str, Any] | None = None,
    ) -> bool:
        await self._write(_DecisionCall(entry=entry, order=order, gateway_state=_plain(gateway_state)))
        return True

    async def update_order(
        self, order: OrderRecord, *, gateway_state: Mapping[str, Any] | None = None
    ) -> None:
        await self._write(_OrderCall(order=order, gateway_state=_plain(gateway_state)))

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
        await self._write(
            _EntryCall(
                orders=tuple(orders),
                fill=fill,
                position=position,
                risk_state=risk_state,
                gateway_state=dict(gateway_state),
                decision=decision,
            )
        )
        return True

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
        await self._write(
            _ExitCall(
                orders=tuple(orders),
                fill=fill,
                trade=trade,
                position=position,
                risk_state=risk_state,
                strategy=strategy,
                risk_amount=risk_amount,
                gateway_state=dict(gateway_state),
                without_position_row=without_position_row,
                halt=halt,
            )
        )

    async def record_position(self, position: ManagedPosition, gateway_state: Mapping[str, Any]) -> None:
        await self._write(_PositionCall(position=position, gateway_state=dict(gateway_state)))

    async def record_control(
        self,
        event: ControlEvent,
        *,
        risk_state: RiskState | None = None,
        gateway_state: Mapping[str, Any] | None = None,
        audit: Mapping[str, Any] | None = None,
    ) -> None:
        await self._write(
            _ControlCall(
                event=event,
                risk_state=risk_state,
                gateway_state=_plain(gateway_state),
                audit=_plain(audit),
            )
        )

    # ------------------------------------------------------------------ gravacoes reconstruiveis (sem fila)

    async def save_state(self, risk_state: RiskState, gateway_state: Mapping[str, Any], at: datetime) -> None:
        if self.down:
            return
        try:
            await self._retry(lambda: self._inner.save_state(risk_state, gateway_state, at))
        except PersistenceUnavailableError as error:
            await self._go_down(error)

    async def record_equity(self, point: EquityPoint) -> bool:
        if self.down:
            return False
        try:
            return await self._retry(lambda: self._inner.record_equity(point))
        except PersistenceUnavailableError as error:
            await self._go_down(error)
            return False


def _plain(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    return None if value is None else dict(value)
