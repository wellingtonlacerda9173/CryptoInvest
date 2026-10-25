"""Cotacao ao vivo SOMENTE VISUAL do painel. Nada aqui chega ao robo nem a decisao de risco.

Le o endpoint publico da Binance (sem chave), com cache curto, uma chamada por par por vez e recuo
exponencial em caso de falha. Cotacao velha demais nunca e entregue como se fosse atual.
"""

import asyncio
import json
import math
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

import aiohttp
import structlog

from api.clock import Clock

log = structlog.get_logger()

CACHE_SECONDS = 2.0
STALE_SECONDS = 15.0
DISCARD_SECONDS = 120.0
BACKOFF_BASE_SECONDS = 2.0
BACKOFF_CAP_SECONDS = 60.0
MAX_TRACKED = 32
MAX_BODY_BYTES = 8192
REJECTED_SECONDS = 60.0
MAX_REJECTED = 256
MAX_UPSTREAM_CONCURRENCY = 4
PROBES_PER_MINUTE = 16


@dataclass(frozen=True, slots=True)
class RawTick:
    price: float
    change_24h_pct: float


@dataclass(frozen=True, slots=True)
class Quote:
    symbol: str
    price: float
    change_24h_pct: float
    as_of: datetime
    age_seconds: float
    stale: bool


class TickerFetcher(Protocol):
    async def __call__(self, symbol: str) -> RawTick: ...


def parse_tick(payload: Any) -> RawTick:
    last = float(payload["lastPrice"])
    open_ = float(payload["openPrice"])
    if not (math.isfinite(last) and math.isfinite(open_)) or last <= 0 or open_ <= 0:
        raise ValueError("cotacao invalida")
    return RawTick(price=last, change_24h_pct=(last - open_) / open_ * 100)


class BinanceTickerFetcher:
    def __init__(self, origin: str, *, timeout_seconds: float = 4.0) -> None:
        self._url = f"{origin.rstrip('/')}/api/v3/ticker/24hr"
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._http: aiohttp.ClientSession | None = None

    async def __call__(self, symbol: str) -> RawTick:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(timeout=self._timeout)
        params = {"symbol": symbol.replace("/", ""), "type": "MINI"}
        async with self._http.get(self._url, params=params, allow_redirects=False) as response:
            response.raise_for_status()
            body = bytearray()
            async for chunk in response.content.iter_chunked(1024):
                body.extend(chunk)
                if len(body) > MAX_BODY_BYTES:
                    raise ValueError("resposta grande demais")
            return parse_tick(json.loads(body))

    async def close(self) -> None:
        if self._http is not None:
            await self._http.close()


def _symbol_rejected(exc: Exception) -> bool:
    if isinstance(exc, aiohttp.ClientResponseError):
        return 400 <= exc.status < 500 and exc.status != 429
    return isinstance(exc, ValueError)


@dataclass(slots=True)
class _Entry:
    tick: RawTick | None = None
    fetched_at: datetime | None = None
    failures: int = 0
    retry_at: datetime | None = None


class TickerService:
    def __init__(self, fetch: TickerFetcher | Callable[[str], Awaitable[RawTick]], clock: Clock) -> None:
        self._fetch = fetch
        self._clock = clock
        self._entries: dict[str, _Entry] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._rejected: dict[str, datetime] = {}
        self._probes: deque[datetime] = deque()
        self._upstream = asyncio.Semaphore(MAX_UPSTREAM_CONCURRENCY)

    async def close(self) -> None:
        close = getattr(self._fetch, "close", None)
        if close is not None:
            await close()

    async def quotes(self, symbols: Sequence[str], *, allowed: frozenset[str] | None = None) -> list[Quote]:
        """`allowed` (pares do robo e posicoes abertas) corta o resto e dispensa o teto de simbolos novos."""
        wanted = [s for s in dict.fromkeys(symbols) if allowed is None or s in allowed]
        found = await asyncio.gather(*(self._quote(s, vouched=allowed is not None) for s in wanted))
        return [q for q in found if q is not None]

    async def _quote(self, symbol: str, *, vouched: bool) -> Quote | None:
        if self._is_rejected(symbol):
            return None
        entry = self._entries.get(symbol)
        if entry is None:
            if not vouched and not self._probe_allowed():
                return None
            if len(self._entries) >= MAX_TRACKED and not self._evict_one():
                return None
            entry = self._entries[symbol] = _Entry()
        if self._due(entry):
            async with self._locks.setdefault(symbol, asyncio.Lock()):
                if self._due(entry):
                    await self._refresh(symbol, entry)
        return self._present(symbol, entry)

    def _is_rejected(self, symbol: str) -> bool:
        until = self._rejected.get(symbol)
        return until is not None and self._clock() < until

    def _probe_allowed(self) -> bool:
        """Teto global de simbolos novos por minuto: par inventado nunca vira chamada ilimitada."""
        now = self._clock()
        while self._probes and (now - self._probes[0]).total_seconds() >= 60:
            self._probes.popleft()
        if len(self._probes) >= PROBES_PER_MINUTE:
            return False
        self._probes.append(now)
        return True

    def _reject(self, symbol: str) -> None:
        now = self._clock()
        for key in [k for k, until in self._rejected.items() if until <= now]:
            del self._rejected[key]
        if len(self._rejected) < MAX_REJECTED:
            self._rejected[symbol] = now + timedelta(seconds=REJECTED_SECONDS)
        self._entries.pop(symbol, None)

    def _evict_one(self) -> bool:
        now = self._clock()
        for key, entry in self._entries.items():
            lock = self._locks.get(key)
            if lock is not None and lock.locked():
                continue
            if entry.fetched_at is None or (now - entry.fetched_at).total_seconds() > DISCARD_SECONDS:
                del self._entries[key]
                self._locks.pop(key, None)
                return True
        return False

    def _due(self, entry: _Entry) -> bool:
        now = self._clock()
        if entry.retry_at is not None and now < entry.retry_at:
            return False
        return entry.fetched_at is None or (now - entry.fetched_at).total_seconds() >= CACHE_SECONDS

    async def _refresh(self, symbol: str, entry: _Entry) -> None:
        try:
            async with self._upstream:
                entry.tick = await self._fetch(symbol)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if entry.tick is None and _symbol_rejected(exc):
                self._reject(symbol)
                return
            entry.failures += 1
            delay = min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS**entry.failures)
            entry.retry_at = self._clock() + timedelta(seconds=delay)
            if entry.failures in (1, 5):
                log.warning(
                    "ticker_indisponivel", symbol=symbol, falhas=entry.failures, erro=type(exc).__name__
                )
            return
        entry.fetched_at = self._clock()
        entry.failures = 0
        entry.retry_at = None

    def _present(self, symbol: str, entry: _Entry) -> Quote | None:
        if entry.tick is None or entry.fetched_at is None:
            return None
        age = max(0.0, (self._clock() - entry.fetched_at).total_seconds())
        if age > DISCARD_SECONDS:
            return None
        return Quote(
            symbol=symbol,
            price=entry.tick.price,
            change_24h_pct=entry.tick.change_24h_pct,
            as_of=entry.fetched_at,
            age_seconds=round(age, 1),
            stale=age > STALE_SECONDS,
        )
