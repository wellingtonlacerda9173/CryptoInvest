"""Barramento de eventos do robo (Redis pub/sub). Indisponivel => o robo continua e registra a degradacao."""

import asyncio
import contextlib
import json
import time
from collections.abc import Mapping
from typing import Any, Protocol

import structlog
from pydantic_core import to_jsonable_python
from redis import asyncio as aioredis
from redis.exceptions import RedisError

from engine.infrastructure.config.logging import scrub_text

log = structlog.get_logger()

CHANNEL_PREFIX = "cryptoinvest:paper"
CHANNELS = ("decisions", "orders", "trades", "state", "equity", "heartbeat", "alerts", "risk")
_RETRY_AFTER_SECONDS = 30.0
_QUEUE_SIZE = 2000
_CLOSE_FLUSH_SECONDS = 3.0


def channel_name(kind: str) -> str:
    return f"{CHANNEL_PREFIX}:{kind}"


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

    def __init__(self, url: str, *, connect_timeout: float = 5.0, io_timeout: float = 2.0) -> None:
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
            body = json.dumps(to_jsonable_python(dict(payload)), separators=(",", ":"))
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
            await self._client.publish(channel_name(kind), body)
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
