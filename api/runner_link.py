"""Ponte entre a API e o runner via Redis: estado atual, comandos assinados e eventos em tempo real.

A API nunca decide nada de trading: ela LÊ o que o runner publica e EMPURRA comandos assinados (HMAC) que o
runner verifica e aplica. Toda falha do Redis vira "indisponível" (nunca exceção para o chamador): o painel
mostra o estado de conexão e os comandos ficam enfileirados no banco até haver entrega.
"""

import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

import structlog
from redis import asyncio as aioredis
from redis.exceptions import RedisError

from engine.runner.paper_events import CHANNELS, RedisKeys
from engine.runner.signing import as_text, open_ack, open_event

log = structlog.get_logger()

_IO_ERRORS = (RedisError, OSError, TimeoutError)
_LATEST_KINDS = ("heartbeat", "state", "equity")
WITHDRAW_SCAN = 1000


class Rejections:
    """Mensagens do Redis sem assinatura valida (forjadas, ou chave diferente): descartadas e contadas.

    O alerta e um log de erro por minuto no maximo (com a contagem), para o Redis nao virar canal de
    inundacao de logs. `total` fica visivel para testes e diagnostico.
    """

    def __init__(self, interval: float = 60.0) -> None:
        self.total = 0
        self._interval = interval
        self._last = float("-inf")
        self._since = 0

    def note(self, what: str) -> None:
        self.total += 1
        self._since += 1
        moment = time.monotonic()
        if moment - self._last >= self._interval:
            log.error("runner_message_rejected", what=what, since_last=self._since, total=self.total)
            self._last, self._since = moment, 0


class FeedDisconnectedError(ConnectionError):
    """A assinatura dos eventos do runner caiu; o consumidor reconecta com espera crescente."""


@dataclass(frozen=True, slots=True)
class Probe:
    """Últimos eventos conhecidos do runner. `reachable=False`: o Redis não respondeu."""

    reachable: bool
    latest: dict[str, dict[str, Any]] = field(default_factory=dict)


class EventSource(Protocol):
    def stream(self) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """Pares `(tipo, payload)` do runner; levanta `FeedDisconnectedError` se a conexão cair."""
        ...


class RunnerLink(Protocol):
    @property
    def keys(self) -> RedisKeys: ...

    async def probe(self) -> Probe: ...

    async def push(self, envelope: str) -> bool:
        """Enfileira o envelope assinado para o runner. Falso se o Redis não aceitou."""
        ...

    async def ack(self, command_id: str) -> dict[str, Any] | None: ...

    async def withdraw(self, command_id: str) -> int:
        """Retira da fila de entrada o envelope (ainda nao consumido) com este id; devolve quantos saíram."""
        ...

    def events(self) -> EventSource: ...

    async def close(self) -> None: ...


def _loads(raw: object) -> dict[str, Any] | None:
    text = as_text(raw)
    if text is None:
        return None
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


def _open_payload(
    raw: object, kind: str, key: bytes | None, rejections: Rejections | None
) -> dict[str, Any] | None:
    """Com chave: so aceita o envelope assinado (qualquer outra coisa e descartada e contada). Sem chave
    (modo sem canal assinado): aceita o JSON puro ou desembrulha o envelope sem verificar."""
    if key is not None:
        payload = open_event(key, kind, raw)
        if payload is None and rejections is not None:
            rejections.note(f"evento {kind} sem assinatura valida")
        return payload
    data = _loads(raw)
    if data is not None and set(data) >= {"payload", "sig"} and isinstance(data["payload"], dict):
        return dict(data["payload"])
    return data


class RedisEventSource:
    def __init__(
        self,
        url: str,
        keys: RedisKeys,
        signing_key: bytes | None = None,
        rejections: Rejections | None = None,
    ) -> None:
        self._url = url
        self._keys = keys
        self._signing_key = signing_key
        self._rejections = rejections

    async def stream(self) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        client: aioredis.Redis = aioredis.from_url(
            self._url, socket_connect_timeout=3, socket_timeout=None, decode_responses=False
        )
        channels = {self._keys.channel(kind): kind for kind in CHANNELS}
        pubsub = client.pubsub()
        try:
            await pubsub.subscribe(*channels)
            while True:
                try:
                    message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=5.0)
                    if message is None:
                        await asyncio.wait_for(client.ping(), 3.0)
                        continue
                except _IO_ERRORS as error:
                    raise FeedDisconnectedError(type(error).__name__) from error
                kind = channels.get(as_text(message.get("channel")) or "")
                payload = (
                    None
                    if kind is None
                    else _open_payload(message.get("data"), kind, self._signing_key, self._rejections)
                )
                if kind is not None and payload is not None:
                    yield kind, payload
        finally:
            try:
                await pubsub.aclose()  # type: ignore[no-untyped-call]
                await client.aclose()
            except _IO_ERRORS:
                log.warning("redis_feed_close_failed")


class RedisRunnerLink:
    def __init__(
        self,
        url: str,
        keys: RedisKeys | None = None,
        *,
        client: "aioredis.Redis | None" = None,
        signing_key: bytes | None = None,
    ) -> None:
        self._url = url
        self._keys = keys or RedisKeys()
        self._signing_key = signing_key
        self.rejections = Rejections()
        self._client: aioredis.Redis = client or aioredis.from_url(
            url, socket_connect_timeout=2, socket_timeout=2, decode_responses=False
        )

    @property
    def keys(self) -> RedisKeys:
        return self._keys

    async def probe(self) -> Probe:
        try:
            values = await self._client.mget([self._keys.latest(kind) for kind in _LATEST_KINDS])
        except _IO_ERRORS:
            return Probe(reachable=False)
        latest = {
            kind: data
            for kind, raw in zip(_LATEST_KINDS, values, strict=True)
            if raw is not None and (data := _open_payload(raw, kind, self._signing_key, self.rejections))
        }
        return Probe(reachable=True, latest=latest)

    async def push(self, envelope: str) -> bool:
        try:
            await self._client.rpush(self._keys.commands, envelope)
        except _IO_ERRORS as error:
            log.warning("command_push_failed", error=type(error).__name__)
            return False
        return True

    async def ack(self, command_id: str) -> dict[str, Any] | None:
        try:
            raw = await self._client.get(self._keys.ack(command_id))
        except _IO_ERRORS:
            return None
        if raw is None:
            return None
        if self._signing_key is None:
            return _loads(raw)
        body = open_ack(self._signing_key, command_id, raw)
        if body is None:
            self.rejections.note("confirmacao de comando sem assinatura valida")
        return body

    async def withdraw(self, command_id: str) -> int:
        removed = 0
        try:
            for item in await self._client.lrange(self._keys.commands, 0, WITHDRAW_SCAN - 1):
                text = as_text(item)
                if text is None or command_id not in text:
                    continue
                body = (_loads(text) or {}).get("payload")
                if isinstance(body, dict) and body.get("id") == command_id:
                    removed += int(await self._client.lrem(self._keys.commands, 1, cast("Any", item)))
        except _IO_ERRORS as error:
            log.warning("command_withdraw_failed", error=type(error).__name__)
        return removed

    def events(self) -> EventSource:
        return RedisEventSource(self._url, self._keys, self._signing_key, self.rejections)

    async def close(self) -> None:
        try:
            await self._client.aclose()
        except _IO_ERRORS:
            log.warning("redis_close_failed")
