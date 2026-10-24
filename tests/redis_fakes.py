"""Redis em memoria para os testes unitarios (so o que o projeto usa) e uma fonte de eventos controlavel."""

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from redis.exceptions import ConnectionError as RedisConnectionError

from api.runner_link import FeedDisconnectedError, Probe


class FakeRedis:
    """Mesmo contrato dos metodos usados: listas, SET NX/EX, MGET, PUBLISH. `broken` simula Redis fora."""

    def __init__(self) -> None:
        self.lists: dict[str, list[str]] = defaultdict(list)
        self.values: dict[str, str] = {}
        self.expiry: dict[str, int] = {}
        self.published: list[tuple[str, str]] = []
        self.broken = False

    def _check(self) -> None:
        if self.broken:
            raise RedisConnectionError("redis fora")

    async def rpush(self, key: str, *values: str) -> int:
        self._check()
        self.lists[key].extend(values)
        return len(self.lists[key])

    async def lpop(self, key: str, count: int | None = None) -> list[str] | None:
        self._check()
        items = self.lists.get(key, [])
        if not items:
            return None
        taken, self.lists[key] = items[: count or 1], items[count or 1 :]
        return taken

    async def llen(self, key: str) -> int:
        self._check()
        return len(self.lists.get(key, []))

    async def lmove(self, first: str, second: str, src: str = "LEFT", dest: str = "RIGHT") -> str | None:
        self._check()
        source = self.lists.get(first, [])
        if not source:
            return None
        item = source.pop(0) if src == "LEFT" else source.pop()
        if dest == "LEFT":
            self.lists[second].insert(0, item)
        else:
            self.lists[second].append(item)
        return item

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        self._check()
        items = self.lists.get(key, [])
        return list(items[start:] if end == -1 else items[start : end + 1])

    async def lrem(self, key: str, count: int, value: str) -> int:
        self._check()
        items = self.lists.get(key, [])
        if value in items:
            items.remove(value)
            return 1
        return 0

    async def set(self, key: str, value: str, nx: bool = False, ex: int | None = None) -> bool | None:
        self._check()
        if nx and key in self.values:
            return None
        self.values[key] = value
        if ex is not None:
            self.expiry[key] = ex
        return True

    async def get(self, key: str) -> str | None:
        self._check()
        return self.values.get(key)

    async def mget(self, keys: list[str]) -> list[str | None]:
        self._check()
        return [self.values.get(k) for k in keys]

    async def publish(self, channel: str, message: str) -> int:
        self._check()
        self.published.append((channel, message))
        return 0

    async def ping(self) -> bool:
        self._check()
        return True

    async def aclose(self) -> None:
        return None


class FakeSource:
    """Fonte de eventos do runner: o teste empurra `(tipo, payload)` e pode derrubar a conexao."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[tuple[str, dict[str, Any]] | Exception] = asyncio.Queue()
        self.opened = 0

    def push(self, kind: str, payload: dict[str, Any]) -> None:
        self.queue.put_nowait((kind, payload))

    def drop(self) -> None:
        self.queue.put_nowait(FeedDisconnectedError("caiu"))

    async def stream(self) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        self.opened += 1
        while True:
            item = await self.queue.get()
            if isinstance(item, Exception):
                raise item
            yield item


class FakeLink:
    """`RunnerLink` em memoria: guarda os envelopes empurrados e devolve o estado programado pelo teste."""

    def __init__(self, source: FakeSource | None = None) -> None:
        from engine.runner.paper_events import RedisKeys

        self.keys = RedisKeys("cryptoinvest:test")
        self.source = source or FakeSource()
        self.latest: dict[str, dict[str, Any]] = {}
        self.reachable = True
        self.accepting = True
        self.pushed: list[str] = []
        self.withdrawn: list[str] = []
        self.acks: dict[str, dict[str, Any]] = {}
        self.signed_now = datetime.now(UTC)

    async def probe(self) -> Probe:
        return Probe(reachable=self.reachable, latest=dict(self.latest) if self.reachable else {})

    async def push(self, envelope: str) -> bool:
        if not (self.reachable and self.accepting):
            return False
        self.pushed.append(envelope)
        return True

    async def ack(self, command_id: str) -> dict[str, Any] | None:
        return self.acks.get(command_id)

    async def withdraw(self, command_id: str) -> int:
        found = [e for e in self.pending if f'"id":"{command_id}"' in e]
        self.withdrawn.extend(found)
        return len(found)

    @property
    def pending(self) -> list[str]:
        """Envelopes empurrados e ainda nao retirados (o que o runner veria na fila)."""
        return [e for e in self.pushed if e not in self.withdrawn]

    def events(self) -> FakeSource:
        return self.source

    async def close(self) -> None:
        return None
