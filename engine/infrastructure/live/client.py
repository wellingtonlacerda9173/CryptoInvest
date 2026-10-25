import asyncio
import json
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from decimal import Decimal
from types import TracebackType
from typing import Any, Self

import aiohttp
import structlog

from engine.infrastructure.binance.backoff import ExponentialBackoff
from engine.infrastructure.config.logging import scrub_text
from engine.infrastructure.live.config import LiveConfig
from engine.infrastructure.live.credentials import ApiCredentials
from engine.infrastructure.live.errors import (
    BinanceApiError,
    ClockDriftError,
    ForbiddenEndpointError,
    IpBannedError,
    OutcomeUnknownError,
    RateLimitedError,
    ReadOnlyModeError,
)
from engine.infrastructure.live.signing import signed_payload

log = structlog.get_logger()

_ALLOWED_ENDPOINTS = frozenset(
    {
        ("GET", "/api/v3/time"),
        ("GET", "/api/v3/exchangeInfo"),
        ("GET", "/api/v3/avgPrice"),
        ("GET", "/api/v3/ticker/price"),
        ("GET", "/api/v3/ticker/bookTicker"),
        ("GET", "/api/v3/account"),
        ("GET", "/api/v3/myTrades"),
        ("GET", "/api/v3/openOrders"),
        ("GET", "/api/v3/order"),
        ("POST", "/api/v3/order"),
        ("DELETE", "/api/v3/order"),
        ("GET", "/api/v3/orderList"),
        ("DELETE", "/api/v3/orderList"),
        ("POST", "/api/v3/orderList/oco"),
        ("POST", "/api/v3/orderList/opoco"),
    }
)
# Em chamadas criticas (protecao/emergencia) um relogio desviado e COMPENSADO pelo offset medido, em vez de
# recusar: recusar deixaria a posicao sem stop. Acima deste teto o desvio e tratado como falha.
_CRITICAL_MAX_DRIFT_MS = 60_000
_TIMESTAMP_ERROR = -1021
_BAN_UNTIL = re.compile(r"banned until (\d{10,})")
_MAX_THROTTLE_WAIT = 65.0
_TRANSIENT = (aiohttp.ClientError, TimeoutError, OSError, OutcomeUnknownError)

Sleep = Callable[[float], Awaitable[None]]
Params = Mapping[str, str | int | Decimal]


class WeightBudget:
    """Peso consumido na janela de 1 minuto (estimativa local, corrigida pelo cabecalho da corretora)."""

    def __init__(self, limit: int, fraction: Decimal) -> None:
        self._ceiling = int(limit * fraction)
        self._events: deque[tuple[float, int]] = deque()

    def used(self, now: float) -> int:
        while self._events and now - self._events[0][0] >= 60:
            self._events.popleft()
        return sum(w for _, w in self._events)

    def wait_needed(self, now: float, weight: int) -> float:
        if self.used(now) + weight <= self._ceiling or not self._events:
            return 0.0
        return max(0.0, 60 - (now - self._events[0][0]))

    def add(self, now: float, weight: int) -> None:
        self._events.append((now, weight))

    def sync_server(self, now: float, server_used: int) -> None:
        if (local := self.used(now)) < server_used:
            self._events.append((now, server_used - local))


class BinanceRestClient:
    """Cliente REST assinado (HMAC-SHA256) restrito a uma allowlist exata de (metodo, caminho) Spot.

    Leituras repetem em falha transitoria. Escritas NUNCA sao reenviadas apos timeout/5xx
    (`OutcomeUnknownError`): quem chama consulta a ordem por `origClientOrderId` antes de reenviar.
    """

    def __init__(
        self,
        config: LiveConfig,
        credentials: ApiCredentials | None,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Sleep = asyncio.sleep,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        if credentials is not None and credentials.environment != config.environment:
            raise ForbiddenEndpointError(
                "credenciais de outro ambiente: chave nao pode cruzar mainnet/testnet"
            )
        self._config = config
        self._credentials = credentials
        self._clock = clock
        self._sleep = sleep
        self._session = session
        self._offset_ms = 0
        self.on_drift: Callable[[int], None] | None = None
        self._synced_at: float | None = None
        self._banned_until = 0.0
        self._budget = WeightBudget(config.weight_limit_per_minute, config.weight_safety_fraction)
        self.write_requests = 0

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    @property
    def offset_ms(self) -> int:
        return self._offset_ms

    async def sync_time(self, *, tolerate_drift: bool = False) -> int:
        """Mede o desvio do relogio local contra /api/v3/time; recusa (ClockDriftError) acima do limite.

        `tolerate_drift` (so chamadas criticas) adota o offset medido ate `_CRITICAL_MAX_DRIFT_MS`.
        """
        before = self._clock()
        payload = await self.request("GET", "/api/v3/time")
        after = self._clock()
        local_ms = int((before + after) / 2 * 1000)
        offset = int(payload["serverTime"]) - local_ms
        limit = _CRITICAL_MAX_DRIFT_MS if tolerate_drift else self._config.max_clock_drift_ms
        if abs(offset) > limit:
            self._synced_at = None
            raise ClockDriftError(f"drift {offset}ms acima do limite {limit}ms")
        self._offset_ms, self._synced_at = offset, self._clock()
        if tolerate_drift and abs(offset) > self._config.max_clock_drift_ms:
            log.error("binance_clock_drift_compensated", offset_ms=offset)
            if self.on_drift is not None:
                self.on_drift(offset)
        return offset

    async def request(
        self,
        method: str,
        path: str,
        params: Params | None = None,
        *,
        signed: bool = False,
        weight: int = 1,
        critical: bool = False,
    ) -> Any:
        """`critical` (protecao/emergencia) ignora a estrangulagem local de peso, nao o banimento."""
        self._guard(method, path)
        if signed:
            await self._ensure_time(critical)
        write = method != "GET"
        resynced = False
        backoff = ExponentialBackoff(base=0.2, cap=5.0)
        for attempt in range(self._config.read_retries + 1):
            await self._throttle(weight, critical)
            try:
                return await self._send(method, path, dict(params or {}), signed, write)
            except BinanceApiError as error:
                if error.code == _TIMESTAMP_ERROR and signed and not resynced:
                    resynced = True
                    await self.sync_time(tolerate_drift=critical)
                    continue
                if error.status == 429:
                    await self._sleep(max(error.retry_after, 0.5))
                    if attempt < self._config.read_retries:
                        continue
                    raise RateLimitedError("429 persistente") from error
                raise
            except _TRANSIENT as error:
                log.warning("binance_request_transient", path=path, error=scrub_text(type(error).__name__))
                if write or attempt >= self._config.read_retries:
                    raise OutcomeUnknownError(f"{method} {path}: {type(error).__name__}") from None
                await self._sleep(backoff.next_delay())
        raise OutcomeUnknownError(f"{method} {path}")

    def _guard(self, method: str, path: str) -> None:
        if method not in ("GET", "POST", "DELETE"):
            raise ForbiddenEndpointError(f"metodo nao permitido: {method}")
        if method != "GET" and self._config.read_only:
            raise ReadOnlyModeError(f"modo somente leitura: {method} {path} bloqueado")
        if (method, path) not in _ALLOWED_ENDPOINTS:
            raise ForbiddenEndpointError(f"endpoint fora da allowlist: {method} {path}")
        if (until := self._banned_until) > self._clock():
            raise IpBannedError(until)

    async def _ensure_time(self, critical: bool) -> None:
        if self._synced_at is None or self._clock() - self._synced_at > self._config.time_resync_seconds:
            await self.sync_time(tolerate_drift=critical)

    async def _throttle(self, weight: int, critical: bool) -> None:
        if critical:
            self._budget.add(self._clock(), weight)
            return
        waited = 0.0
        while (need := self._budget.wait_needed(self._clock(), weight)) > 0:
            if waited + need > _MAX_THROTTLE_WAIT:
                raise RateLimitedError("orcamento de peso esgotado")
            await self._sleep(need)
            waited += need
        self._budget.add(self._clock(), weight)

    def _http(self) -> aiohttp.ClientSession:
        if self._session is None:
            timeout = aiohttp.ClientTimeout(total=self._config.request_timeout_seconds)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def _send(self, method: str, path: str, params: dict[str, Any], signed: bool, write: bool) -> Any:
        headers: dict[str, str] = {}
        url = f"{self._config.base_url.rstrip('/')}{path}"
        body: str | None = None
        if signed:
            if self._credentials is None:
                raise ForbiddenEndpointError("endpoint assinado sem credenciais")
            headers["X-MBX-APIKEY"] = self._credentials.api_key
            params["timestamp"] = int(self._clock() * 1000) + self._offset_ms
            params["recvWindow"] = self._config.recv_window_ms
            payload = signed_payload(params, self._credentials.api_secret)
        else:
            payload = "&".join(f"{k}={v}" for k, v in params.items())
        if write:
            body = payload
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            self.write_requests += 1
        elif payload:
            url = f"{url}?{payload}"
        async with self._http().request(
            method, url, data=body, headers=headers, allow_redirects=False
        ) as response:
            if 300 <= response.status < 400:
                raise ForbiddenEndpointError(f"redirecionamento recusado: HTTP {response.status}")
            raw = await response.text()
            self._after_response(response.status, response.headers)
            if response.status >= 500:
                raise OutcomeUnknownError(f"{method} {path}: HTTP {response.status}")
            data = _parse(raw, strict=response.status < 400)
            code = int(data.get("code", 0) or 0) if isinstance(data, dict) else 0
            if response.status >= 400 or code < 0:
                msg = str(data.get("msg", "")) if isinstance(data, dict) else raw[:200]
                if response.status == 418:
                    self._ban(response.headers, msg)
                raise BinanceApiError(
                    response.status, code, scrub_text(msg), _header_seconds(response.headers, 0.0)
                )
            return data

    def _after_response(self, status: int, headers: Mapping[str, str]) -> None:
        if (used := headers.get("x-mbx-used-weight-1m")) is not None and used.isdigit():
            self._budget.sync_server(self._clock(), int(used))

    def _ban(self, headers: Mapping[str, str], message: str) -> None:
        if match := _BAN_UNTIL.search(message):
            self._banned_until = max(self._banned_until, int(match.group(1)) / 1000)
        else:
            self._banned_until = max(self._banned_until, self._clock() + _header_seconds(headers, 60.0))
        log.error("binance_ip_banned", until=self._banned_until)


def _header_seconds(headers: Mapping[str, str], default: float) -> float:
    value = headers.get("Retry-After")
    return float(value) if value and value.replace(".", "", 1).isdigit() else default


def _parse(raw: str, *, strict: bool) -> Any:
    """2xx com corpo nao-JSON (proxy/CDN/captive portal) NUNCA e sucesso: resultado desconhecido."""
    try:
        return json.loads(raw)
    except ValueError:
        if strict:
            raise OutcomeUnknownError("resposta 2xx com corpo nao-JSON") from None
        return {"msg": raw[:200]}
