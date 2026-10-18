import asyncio
import json
import re
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import aclosing
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any, Self

import aiohttp
import ccxt.async_support as ccxt
import structlog

from engine.domain.candle import Candle
from engine.domain.market import OrderBookSnapshot
from engine.domain.types import Symbol, Timeframe
from engine.infrastructure.binance.backoff import ExponentialBackoff
from engine.infrastructure.binance.mapping import (
    from_millis,
    parse_depth,
    parse_rest_kline,
    parse_ws_kline,
    stream_name,
    to_exchange_symbol,
    to_millis,
)

log = structlog.get_logger()

_KLINES_PAGE = 1000
_RECONNECTABLE = (aiohttp.ClientError, TimeoutError, OSError)
_RECOVERY_ERRORS = (ccxt.NetworkError, ccxt.ExchangeError, *_RECONNECTABLE)
_PERMANENT_ERRORS = (ccxt.BadRequest, ccxt.AuthenticationError, ccxt.NotSupported, ccxt.ArgumentsRequired)
_RECOVERY_ATTEMPTS = 5
_MALFORMED = (KeyError, TypeError, ValueError, ArithmeticError)
_BAD_ROW = (*_MALFORMED, IndexError)
_BAN_UNTIL = re.compile(r"banned until (\d{10,})")
# formato do ccxt: "<exchange> <METODO> <url> <status> <motivo> <corpo>"; ancora o status logo apos a URL
_HTTP_451 = re.compile(r"^\S+ [A-Z]+ \S+ 451(?:\s|$)")


class RegionBlockedError(RuntimeError):
    """HTTP 451: a corretora bloqueia este IP/regiao. Repetir nao adianta."""


class MarketDataConfigError(RuntimeError):
    """Erro permanente na recuperacao REST (simbolo/intervalo invalido, credencial, recurso nao suportado)."""


class BinanceMarketData:
    """Dados de mercado publicos da Binance (sem chave): REST historico e WebSocket."""

    def __init__(
        self,
        rest_url: str = "https://api.binance.com/api/v3",
        ws_url: str = "wss://stream.binance.com:9443",
    ) -> None:
        self._ws_url = ws_url.rstrip("/")
        self._exchange = ccxt.binance({"enableRateLimit": True})
        self._exchange.urls["api"]["public"] = rest_url.rstrip("/")

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._exchange.close()

    async def server_time(self) -> datetime:
        response = await self._exchange.publicGetTime()
        return from_millis(int(response["serverTime"]))

    async def fetch_candles(
        self, symbol: Symbol, timeframe: Timeframe, start: datetime, end: datetime | None = None
    ) -> list[Candle]:
        """Pagina o endpoint de klines; devolve apenas velas fechadas, em ordem crescente."""
        if start.tzinfo is None or (end is not None and end.tzinfo is None):
            raise ValueError("start/end must be timezone-aware")
        server_now = await self.server_time()
        end = min(end, server_now) if end else server_now
        step_ms = timeframe.seconds * 1000
        cursor, end_ms = to_millis(start), to_millis(end)
        candles: list[Candle] = []
        while cursor < end_ms:
            rows: list[list[Any]] = await self._exchange.publicGetKlines(
                {
                    "symbol": to_exchange_symbol(symbol),
                    "interval": timeframe.value,
                    "startTime": cursor,
                    "endTime": end_ms,
                    "limit": _KLINES_PAGE,
                }
            )
            if not rows:
                break
            parsed = (parse_rest_kline(symbol, timeframe, row, server_now) for row in rows)
            candles.extend(c for c in parsed if c.closed)
            cursor = int(rows[-1][0]) + step_ms
            if len(rows) < _KLINES_PAGE:
                break
        return candles

    async def stream_candles(
        self, symbol: Symbol, timeframe: Timeframe, since: datetime | None = None
    ) -> AsyncIterator[Candle]:
        """Emite somente velas fechadas. Apos (re)conexao, recupera via REST as velas perdidas.

        Com `since`, a primeira conexao ja recupera as velas fechadas desde esse instante.
        """
        if since is not None and since.tzinfo is None:
            raise ValueError("since must be timezone-aware")
        step = timedelta(seconds=timeframe.seconds)
        url = f"{self._ws_url}/stream?streams={stream_name(symbol)}@kline_{timeframe.value}"
        last_open: datetime | None = None
        anchor = since
        pending_gap_start: datetime | None = None
        async with aclosing(self._messages(url)) as messages:
            async for message in messages:
                if message is None:
                    if anchor is None:
                        # margem de uma vela: o relogio local pode estar adiantado
                        anchor = _floor(datetime.now(UTC), timeframe) - step
                        continue
                    start = pending_gap_start or (last_open + step if last_open else anchor)
                    recovered = await self._recover(symbol, timeframe, start)
                    if recovered is None:
                        pending_gap_start = start
                        log.error(
                            "binance_candle_gap",
                            symbol=symbol,
                            timeframe=timeframe.value,
                            from_open_time=start.isoformat(),
                            hint="recuperacao REST falhou; nova tentativa ao receber a proxima vela ao vivo",
                        )
                        continue
                    pending_gap_start = None
                    for candle in recovered:
                        if last_open is None or candle.open_time > last_open:
                            last_open = candle.open_time
                            yield candle
                    continue
                try:
                    closed = parse_ws_kline(symbol, timeframe, message["data"])
                except _MALFORMED as exc:
                    log.warning("binance_ws_message_discarded", error=repr(exc))
                    continue
                if closed is None or (last_open is not None and closed.open_time <= last_open):
                    continue
                if pending_gap_start is not None:
                    # A vela ao vivo so e emitida depois do backfill, para manter a ordem crescente.
                    # Se esta ultima tentativa falhar, a lacuna e abandonada: preencher depois
                    # emitiria velas antigas apos velas novas.
                    backfill = await self._recover(symbol, timeframe, pending_gap_start, attempts=1)
                    if backfill is None:
                        log.error(
                            "binance_candle_gap_abandoned",
                            symbol=symbol,
                            timeframe=timeframe.value,
                            from_open_time=pending_gap_start.isoformat(),
                            hint="backfill necessario via fetch_candles",
                        )
                    else:
                        for candle in backfill:
                            if candle.open_time < closed.open_time and (
                                last_open is None or candle.open_time > last_open
                            ):
                                last_open = candle.open_time
                                yield candle
                    pending_gap_start = None
                last_open = closed.open_time
                yield closed

    async def stream_order_book(self, symbol: Symbol, depth: int = 10) -> AsyncIterator[OrderBookSnapshot]:
        url = f"{self._ws_url}/stream?streams={stream_name(symbol)}@depth{depth}@100ms"
        async with aclosing(self._messages(url)) as messages:
            async for message in messages:
                if message is None:
                    continue
                try:
                    book = parse_depth(symbol, message["data"], datetime.now(UTC))
                except _MALFORMED as exc:
                    log.warning("binance_ws_message_discarded", error=repr(exc))
                    continue
                yield book

    async def _recover(
        self, symbol: Symbol, timeframe: Timeframe, start: datetime, attempts: int = _RECOVERY_ATTEMPTS
    ) -> list[Candle] | None:
        """Backfill via REST com retry. `None` significa lacuna: o chamador decide como tratar.

        Politica de erros:
        - rede, rate limit, 5xx e demais `ExchangeError` transitorios: retry com backoff;
        - linha malformada (parse/dados invalidos): loga e devolve `None` sem retry;
        - HTTP 451: `RegionBlockedError`; simbolo/intervalo invalido, credencial ou recurso
          nao suportado (BadRequest/BadSymbol/AuthenticationError/NotSupported):
          `MarketDataConfigError`, pois repetir nao adianta.
        """
        backoff = ExponentialBackoff()
        for attempt in range(1, attempts + 1):
            try:
                return await self.fetch_candles(symbol, timeframe, start)
            except _BAD_ROW as exc:
                log.error("binance_rest_data_invalid", symbol=symbol, error=repr(exc))
                return None
            except _PERMANENT_ERRORS as exc:
                log.error("binance_rest_config_error", symbol=symbol, error=repr(exc))
                raise MarketDataConfigError(f"permanent REST error for {symbol}: {exc}") from exc
            except _RECOVERY_ERRORS as exc:
                if isinstance(exc, ccxt.ExchangeNotAvailable) and _HTTP_451.search(str(exc)):
                    log.error("binance_rest_region_blocked", error=str(exc))
                    raise RegionBlockedError("Binance returned HTTP 451 (region/IP blocked)") from exc
                delay = backoff.next_delay(self._forced_wait(exc))
                log.warning("binance_recovery_retry", attempt=attempt, delay=round(delay, 2), error=repr(exc))
                if attempt < attempts:
                    await asyncio.sleep(delay)
        return None

    def _forced_wait(self, exc: BaseException) -> float:
        """Espera minima imposta pela corretora em 429/418 (Retry-After ou 'banned until')."""
        if not isinstance(exc, (ccxt.DDoSProtection, ccxt.RateLimitExceeded)):
            return 0.0
        headers: dict[str, str] = getattr(self._exchange, "last_response_headers", None) or {}
        retry_after = next((v for k, v in headers.items() if k.lower() == "retry-after"), None)
        if retry_after and retry_after.isdigit():
            return float(retry_after)
        if match := _BAN_UNTIL.search(str(exc)):
            return max(0.0, int(match[1]) / 1000 - datetime.now(UTC).timestamp())
        return 0.0

    async def _messages(self, url: str) -> AsyncGenerator[dict[str, Any] | None]:
        """Mantem a conexao; emite `None` a cada (re)conexao bem-sucedida e depois as mensagens validas."""
        backoff = ExponentialBackoff()
        async with aiohttp.ClientSession() as session:
            while True:
                try:
                    async with session.ws_connect(
                        url, heartbeat=20, timeout=aiohttp.ClientWSTimeout(ws_receive=60)
                    ) as ws:
                        log.info("binance_ws_connected", url=url)
                        backoff.reset()
                        yield None
                        async for raw in ws:
                            if raw.type is aiohttp.WSMsgType.TEXT:
                                if (payload := _decode(raw.data)) is not None:
                                    yield payload
                            elif raw.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                break
                except _RECONNECTABLE as exc:
                    log.warning("binance_ws_error", url=url, error=repr(exc))
                delay = backoff.next_delay()
                log.info("binance_ws_reconnect", delay=round(delay, 2))
                await asyncio.sleep(delay)


def _decode(text: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(text)
    except ValueError:
        log.warning("binance_ws_invalid_json")
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        log.warning("binance_ws_unexpected_message")
        return None
    return payload


def _floor(moment: datetime, timeframe: Timeframe) -> datetime:
    seconds = timeframe.seconds
    return datetime.fromtimestamp(int(moment.timestamp()) // seconds * seconds, tz=UTC)
