"""Tempo real: eventos do runner (Redis pub/sub) distribuídos aos clientes do WebSocket.

Um único consumidor do Redis alimenta todos os clientes. Cada cliente tem uma fila LIMITADA: se ele não dá
conta (rede lenta), as mensagens mais antigas são descartadas, ele recebe um `resync` (refaça a leitura por
REST) e, passando do limite de descartes, é desconectado (código 1013). Um anel com as últimas mensagens
permite retomar por `last_event_id` na mesma época da API (reinício da API = nova época = `resync`).
"""

import asyncio
import re
import secrets
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog

from api.runner_link import EventSource, FeedDisconnectedError, RunnerLink
from api.services.runner_status import MAX_CLOCK_AHEAD
from api.ws_schema import (
    RUNNER_TO_CHANNEL,
    ChannelName,
    EventMessage,
    ResyncMessage,
    RunnerMessage,
)

log = structlog.get_logger()

DISCONNECTED_TEXT = "Desconectado do robô: sem sinal recente. Os números podem estar desatualizados."
CONNECTED_TEXT = "Conectado ao robô."
# teto global de conexões (o limite por usuário sozinho não segura muitos usuários); acima disso, 1008
MAX_TOTAL_CONNECTIONS = 200
FEED_DOWN_TEXT = "Sem conexão com o canal de eventos do robô. Os números podem estar desatualizados."


def frame(message: Any) -> str:
    return str(message.model_dump_json())


@dataclass(frozen=True, slots=True)
class FeedEvent:
    seq: int
    channel: ChannelName
    frame: str


@dataclass(slots=True, eq=False)
class Subscription:
    """Um cliente conectado. A fila guarda quadros JSON já prontos; o envio é feito pelo handler do `/ws`."""

    user_id: str
    queue: asyncio.Queue[str]
    max_dropped: int
    channels: set[ChannelName] = field(default_factory=set)
    dropped: int = 0
    needs_resync: bool = False
    overflowed: asyncio.Event = field(default_factory=asyncio.Event)

    def offer(self, channel: ChannelName | None, quadro: str) -> None:
        """`channel=None`: mensagem de controle (vai para todos). Fila cheia descarta a mais antiga."""
        if channel is not None and channel not in self.channels:
            return
        if self.queue.full():
            _discard_oldest(self.queue)
            self.dropped += 1
            self.needs_resync = True
            if self.dropped > self.max_dropped:
                self.overflowed.set()
        self.queue.put_nowait(quadro)


def _discard_oldest(queue: asyncio.Queue[str]) -> None:
    try:
        queue.get_nowait()
    except asyncio.QueueEmpty:
        return


_EVENT_ID = re.compile(r'"id"\s*:\s*"[0-9a-f]+:(\d+)"')


def event_seq(text: str) -> int | None:
    """Sequência de um quadro de evento (`"id":"<época>:<n>"`); quadros de controle não têm."""
    found = _EVENT_ID.search(text, 0, 160)
    return int(found.group(1)) if found else None


class RealtimeHub:
    def __init__(
        self,
        source: EventSource | None,
        *,
        clock: Callable[[], datetime],
        link: RunnerLink | None = None,
        buffer_size: int = 1000,
        stale_seconds: float = 90.0,
        queue_size: int = 200,
        max_dropped: int = 100,
        max_total: int = MAX_TOTAL_CONNECTIONS,
    ) -> None:
        self._max_total = max_total
        self.epoch = secrets.token_hex(4)
        self._source = source
        self._link = link
        self._clock = clock
        self._ring: deque[FeedEvent] = deque(maxlen=buffer_size)
        self._subs: set[Subscription] = set()
        self._per_user: Counter[str] = Counter()
        self._seq = 0
        self._stale = stale_seconds
        self._queue_size = queue_size
        self._max_dropped = max_dropped
        self._last_runner_event: datetime | None = None
        self._connected = False
        self._source_down = False

    # ------------------------------------------------------------------ estado

    @property
    def runner_connected(self) -> bool:
        return self._connected

    @property
    def clients(self) -> int:
        return len(self._subs)

    def last_event_id(self) -> str:
        return f"{self.epoch}:{self._seq}"

    # ------------------------------------------------------------------ clientes

    def register(self, user_id: str, limit: int) -> Subscription | None:
        if len(self._subs) >= self._max_total or self._per_user[user_id] >= limit:
            return None
        sub = Subscription(user_id, asyncio.Queue(self._queue_size), self._max_dropped)
        self._subs.add(sub)
        self._per_user[user_id] += 1
        return sub

    def unregister(self, sub: Subscription) -> None:
        if sub in self._subs:
            self._subs.discard(sub)
            self._per_user[sub.user_id] -= 1
            if self._per_user[sub.user_id] <= 0:
                del self._per_user[sub.user_id]

    def replay(self, last_event_id: str | None, channels: set[ChannelName]) -> tuple[bool, list[str]]:
        """(retomou, quadros perdidos). Sem `last_event_id` válido da época atual não há retomada."""
        if not last_event_id:
            return False, []
        epoch, _, raw = last_event_id.partition(":")
        if epoch != self.epoch or not raw.isdigit():
            return False, []
        last = int(raw)
        if last > self._seq:
            return False, []
        oldest = self._ring[0].seq if self._ring else self._seq + 1
        if last + 1 < oldest:
            return False, []
        return True, [e.frame for e in self._ring if e.seq > last and e.channel in channels]

    # ------------------------------------------------------------------ entrada

    def ingest(self, kind: str, payload: dict[str, Any]) -> None:
        channel = RUNNER_TO_CHANNEL.get(kind)
        if channel is None:
            return
        now = self._clock()
        if _from_the_future(payload, now):
            log.error("realtime_event_from_the_future", kind=kind)
            return
        self._source_down = False
        self._last_runner_event = now
        self._set_connected(True)
        self._seq += 1
        message = EventMessage(
            id=f"{self.epoch}:{self._seq}", channel=channel, at=_event_time(payload, now), data=payload
        )
        event = FeedEvent(self._seq, channel, frame(message))
        self._ring.append(event)
        for sub in list(self._subs):
            sub.offer(channel, event.frame)

    def check_stale(self) -> None:
        """Chamado periodicamente: sem sinal do runner além do limite = aviso de desconexão."""
        last = self._last_runner_event
        if last is None or (self._clock() - last).total_seconds() > self._stale:
            self._set_connected(False)

    def _set_connected(self, connected: bool) -> None:
        if connected == self._connected:
            return
        self._connected = connected
        text = CONNECTED_TEXT if connected else self._down_text()
        notice = frame(
            RunnerMessage(connected=connected, message=text, last_heartbeat_at=self._last_runner_event)
        )
        for sub in list(self._subs):
            sub.offer(None, notice)

    def _down_text(self) -> str:
        return FEED_DOWN_TEXT if self._source_down else DISCONNECTED_TEXT

    def notice(self) -> str:
        text = CONNECTED_TEXT if self._connected else self._down_text()
        return frame(
            RunnerMessage(connected=self._connected, message=text, last_heartbeat_at=self._last_runner_event)
        )

    @staticmethod
    def resync_frame(reason: str) -> str:
        return frame(ResyncMessage(reason=reason))

    # ------------------------------------------------------------------ tarefas de fundo

    async def run(self) -> None:
        """Consome o Redis para sempre; queda da conexão = tenta de novo com espera crescente."""
        if self._source is None:
            return
        await self._seed()
        delay = 1.0
        while True:
            try:
                async for kind, payload in self._source.stream():
                    delay = 1.0
                    self.ingest(kind, payload)
            except FeedDisconnectedError:
                log.warning("realtime_feed_disconnected")
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log.error("realtime_feed_failed", error=type(error).__name__)
            self._source_down = True
            self._set_connected(False)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)

    async def _seed(self) -> None:
        if self._link is None:
            return
        probe = await self._link.probe()
        for kind in ("heartbeat", "state"):
            payload = probe.latest.get(kind)
            if payload is not None:
                self.ingest(kind, payload)
                return

    async def monitor(self, every: float = 5.0) -> None:
        while True:
            await asyncio.sleep(every)
            self.check_stale()


def _from_the_future(payload: dict[str, Any], now: datetime) -> bool:
    """`heartbeat_at` adiantado alem da folga de relogio: suspeito (nao conta como sinal de vida)."""
    raw = payload.get("heartbeat_at")
    try:
        moment = datetime.fromisoformat(str(raw))
    except ValueError:
        return False
    moment = moment if moment.tzinfo else moment.replace(tzinfo=UTC)
    return moment - now > MAX_CLOCK_AHEAD


def _event_time(payload: dict[str, Any], fallback: datetime) -> datetime:
    raw = payload.get("at") or payload.get("heartbeat_at")
    try:
        moment = datetime.fromisoformat(str(raw))
    except ValueError:
        return fallback
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)
