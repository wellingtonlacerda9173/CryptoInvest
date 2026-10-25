"""Persistencia continua de velas fechadas e backfill de lacunas via REST publico de klines."""

from collections.abc import MutableSet, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

import structlog

from engine.domain.candle import Candle
from engine.domain.types import Symbol, Timeframe
from engine.infrastructure.config.logging import scrub_text
from engine.runner.candle_source import floor_to_timeframe, missing_ranges

log = structlog.get_logger()

BACKFILL_LIMIT = 1000


class CandleStore(Protocol):
    async def upsert_many(self, candles: Sequence[Candle]) -> int: ...

    async def fetch_range(
        self, symbol: Symbol, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[Candle]: ...


class CandleFetcher(Protocol):
    async def fetch_candles(
        self, symbol: Symbol, timeframe: Timeframe, start: datetime, end: datetime | None = None
    ) -> list[Candle]: ...


@dataclass(frozen=True)
class BackfillResult:
    symbol: Symbol
    gaps: int
    persisted: int
    unrecovered: int
    reconciled: int = 0


GapKey = tuple[Symbol, datetime, datetime]


async def persist_closed(store: CandleStore, candles: Sequence[Candle]) -> bool:
    """Upsert idempotente; falha de banco nunca derruba o robo (o proximo backfill refaz)."""
    closed = [c for c in candles if c.closed]
    if not closed:
        return True
    try:
        await store.upsert_many(closed)
    except Exception as error:
        log.error("candle_persist_failed", count=len(closed), error=scrub_text(repr(error)))
        return False
    return True


def _differs(a: Candle, b: Candle) -> bool:
    return (a.open, a.high, a.low, a.close, a.volume) != (b.open, b.high, b.low, b.close, b.volume)


async def _reconcile(
    store: CandleStore,
    market: CandleFetcher,
    stored: dict[datetime, Candle],
    symbol: Symbol,
    timeframe: Timeframe,
    start: datetime,
    end: datetime,
) -> int:
    """Velas finais da Binance sao imutaveis: onde o banco diverge da REST, a REST vence."""
    fetched = [
        c
        for c in await market.fetch_candles(symbol, timeframe, start, end)
        if c.closed and start <= c.open_time < end
    ]
    fixes = [c for c in fetched if (old := stored.get(c.open_time)) is not None and _differs(old, c)]
    if fixes and await persist_closed(store, fixes):
        stored.update({c.open_time: c for c in fixes})
        log.warning("candle_store_reconciled", symbol=symbol, timeframe=timeframe.value, corrected=len(fixes))
        return len(fixes)
    return 0


async def backfill_store(
    store: CandleStore,
    market: CandleFetcher,
    symbol: Symbol,
    timeframe: Timeframe,
    now: datetime,
    *,
    limit: int = BACKFILL_LIMIT,
    reconcile: bool = False,
    known_gaps: MutableSet[GapKey] | None = None,
) -> BackfillResult:
    """Preenche as velas fechadas ausentes das ultimas `limit`; o rate limit e do cliente REST.

    Com `reconcile`, uma pagina REST corrige velas divergentes. `known_gaps` guarda lacunas ja tentadas sem
    retorno (manutencao historica da corretora): nao sao buscadas nem contadas de novo.
    """
    step = timedelta(seconds=timeframe.seconds)
    end = floor_to_timeframe(now, timeframe)
    start = end - step * limit
    known = known_gaps if known_gaps is not None else set[GapKey]()
    reconciled = persisted = unrecovered = skipped = 0
    try:
        stored = {c.open_time: c for c in await store.fetch_range(symbol, timeframe, start, end)}
        if reconcile:
            reconciled = await _reconcile(store, market, stored, symbol, timeframe, start, end)
        gaps = missing_ranges(sorted(stored.values(), key=lambda c: c.open_time), start, end, timeframe)
        for first, last in gaps:
            if any(sym == symbol and f <= first and last <= end_ for sym, f, end_ in known):
                skipped += 1
                continue
            fetched = [
                c
                for c in await market.fetch_candles(symbol, timeframe, first, last + step)
                if c.closed and first <= c.open_time <= last
            ]
            if await persist_closed(store, fetched):
                persisted += len(fetched)
            expected = int((last - first) / step) + 1
            if last == end - step and all(c.open_time != last for c in fetched):
                # o relogio do robo pode estar adiantado: a ultima vela ainda pode nao existir na corretora
                expected -= 1
            missing = max(expected - len(fetched), 0)
            if missing:
                known.add((symbol, first, last))
                unrecovered += missing
    except Exception as error:
        log.error("candle_backfill_failed", symbol=symbol, error=scrub_text(repr(error)))
        return BackfillResult(symbol, 0, 0, limit, reconciled)
    if unrecovered:
        log.error("candle_store_gap", symbol=symbol, timeframe=timeframe.value, missing=unrecovered)
    elif skipped:
        log.warning("candle_store_gap_known", symbol=symbol, timeframe=timeframe.value, gaps=skipped)
    elif gaps:
        log.warning("candle_store_backfilled", symbol=symbol, gaps=len(gaps), candles=persisted)
    return BackfillResult(symbol, len(gaps), persisted, unrecovered, reconciled)
