"""Barramento de eventos do robo (Redis pub/sub). Indisponivel => o robo continua e registra a degradacao."""

import asyncio
import contextlib
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import structlog
from pydantic_core import to_jsonable_python
from redis import asyncio as aioredis
from redis.exceptions import RedisError

from engine.infrastructure.config.logging import scrub_text
from engine.runner.signing import wrap_event

log = structlog.get_logger()

CHANNEL_PREFIX = "cryptoinvest:paper"
CHANNELS = ("decisions", "orders", "trades", "state", "equity", "heartbeat", "alerts", "risk")
# ultimo valor de alguns canais, em chave com TTL: a API le o estado atual sem esperar o proximo evento
LATEST_KINDS = frozenset({"state", "heartbeat", "equity"})
LATEST_TTL_SECONDS = 900
_RETRY_AFTER_SECONDS = 30.0
_QUEUE_SIZE = 2000
_CLOSE_FLUSH_SECONDS = 3.0


def channel_name(kind: str) -> str:
    return f"{CHANNEL_PREFIX}:{kind}"


@dataclass(frozen=True, slots=True)
class RedisKeys:
    """Nomes de canais e chaves Redis do robo. O prefixo isola ambientes (testes usam outro) e e o MESMO
    contrato entre runner (publica, consome comandos) e API (assina, enfileira comandos)."""

    prefix: str = CHANNEL_PREFIX

    def channel(self, kind: str) -> str:
        return f"{self.prefix}:{kind}"

    def latest(self, kind: str) -> str:
        return f"{self.prefix}:last:{kind}"

    @property
    def commands(self) -> str:
        return f"{self.prefix}:commands"

    @property
    def processing(self) -> str:
        """Envelopes ja retirados de `commands` e ainda nao confirmados como duraveis (at-least-once)."""
        return f"{self.prefix}:commands:processing"

    def nonce(self, command_id: str) -> str:
        return f"{self.prefix}:cmd:nonce:{command_id}"

    def ack(self, command_id: str) -> str:
        return f"{self.prefix}:cmd:ack:{command_id}"


class EventBus(Protocol):
    @property
    def healthy(self) -> bool: ...

    async def publish(self, kind: str, payload: Mapping[str, Any]) -> None:
        """Nunca levanta nem bloqueia: falha de entrega vira log e degradacao."""
        ...

    async def close(self) -> None: ...


class NullEventBus:
    healthy = False

    async def publish(self, kind: str, payload: Mapping[str, Any]) -> None:
        return None

    async def close(self) -> None:
        return None


class RedisEventBus:
    """Publica JSON em `cryptoinvest:paper:<tipo>` por fila em segundo plano (o robo nunca espera o Redis).

    Apos uma falha pausa as tentativas por 30 s e descarta o que chegar (os eventos tambem estao no diario).
    """

    def __init__(
        self,
        url: str,
        *,
        connect_timeout: float = 5.0,
        io_timeout: float = 2.0,
        keys: RedisKeys | None = None,
        signing_key: bytes | None = None,
    ) -> None:
        self._keys = keys or RedisKeys()
        self._signing_key = signing_key
        self._client: aioredis.Redis = aioredis.from_url(
            url, socket_connect_timeout=connect_timeout, socket_timeout=io_timeout, decode_responses=True
        )
        self._queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue(maxsize=_QUEUE_SIZE)
        self._worker: asyncio.Task[None] | None = None
        self._down_until = 0.0
        self.healthy = True
        self.failures = 0
        self.dropped = 0

    async def publish(self, kind: str, payload: Mapping[str, Any]) -> None:
        try:
            data = to_jsonable_python(dict(payload))
            body = (
                json.dumps(data, separators=(",", ":"))
                if self._signing_key is None
                else wrap_event(self._signing_key, kind, data)
            )
        except (TypeError, ValueError) as error:
            log.warning("event_not_serializable", kind=kind, error=scrub_text(repr(error)))
            return
        if self._worker is None:
            self._worker = asyncio.create_task(self._drain())
        try:
            self._queue.put_nowait((kind, body))
        except asyncio.QueueFull:
            self.dropped += 1

    async def _drain(self) -> None:
        while True:
            kind, body = await self._queue.get()
            try:
                await self._send(kind, body)
            finally:
                self._queue.task_done()

    async def _send(self, kind: str, body: str) -> None:
        if time.monotonic() < self._down_until:
            self.dropped += 1
            return
        try:
            await self._client.publish(self._keys.channel(kind), body)
            if kind in LATEST_KINDS:
                await self._client.set(self._keys.latest(kind), body, ex=LATEST_TTL_SECONDS)
        except (RedisError, OSError, TimeoutError) as error:
            self.failures += 1
            self._down_until = time.monotonic() + _RETRY_AFTER_SECONDS
            if self.healthy:
                log.error("redis_unavailable", kind=kind, error=scrub_text(repr(error)), degraded=True)
            self.healthy = False
            return
        if not self.healthy:
            log.info("redis_recovered")
        self.healthy = True

    async def close(self) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._queue.join(), _CLOSE_FLUSH_SECONDS)
        if self._worker is not None:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker
        try:
            await self._client.aclose()
        except (RedisError, OSError, TimeoutError):
            log.warning("redis_close_failed")
