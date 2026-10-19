import csv
import socket
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from pathlib import Path

from engine.domain.candle import Candle
from engine.domain.types import Symbol, Timeframe
from engine.infrastructure.binance.market_data import BinanceMarketData
from engine.infrastructure.config.settings import Settings, load_settings
from engine.infrastructure.persistence.candle_repository import CandleRepository
from engine.infrastructure.persistence.session import create_engine, create_session_factory

_CACHE_HEADER = ["open_time", "open", "high", "low", "close", "volume"]
_DB_TIMEOUT = 3.0


@dataclass(frozen=True)
class LoadedCandles:
    candles: list[Candle]
    source: str
    notes: tuple[str, ...]


def floor_to_timeframe(moment: datetime, timeframe: Timeframe) -> datetime:
    step = timeframe.seconds
    return datetime.fromtimestamp(int(moment.timestamp()) // step * step, tz=UTC)


def cache_path(cache_dir: Path, symbol: Symbol, timeframe: Timeframe) -> Path:
    return cache_dir / f"{symbol.replace('/', '')}_{timeframe.value}.csv"


def read_cache(path: Path, symbol: Symbol, timeframe: Timeframe) -> list[Candle]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as handle:
        return [
            Candle(
                symbol=symbol,
                timeframe=timeframe,
                open_time=datetime.fromisoformat(row["open_time"]),
                open=Decimal(row["open"]),
                high=Decimal(row["high"]),
                low=Decimal(row["low"]),
                close=Decimal(row["close"]),
                volume=Decimal(row["volume"]),
                closed=True,
            )
            for row in csv.DictReader(handle)
        ]


def write_cache(path: Path, candles: Iterable[Candle]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(_CACHE_HEADER)
        writer.writerows(
            [c.open_time.isoformat(), c.open, c.high, c.low, c.close, c.volume] for c in candles if c.closed
        )


def merge_candles(*sources: Iterable[Candle]) -> list[Candle]:
    merged: dict[datetime, Candle] = {}
    for source in sources:
        for candle in source:
            merged[candle.open_time] = candle
    return [merged[key] for key in sorted(merged)]


def missing_ranges(
    candles: Sequence[Candle], start: datetime, end: datetime, timeframe: Timeframe
) -> list[tuple[datetime, datetime]]:
    """Intervalos [a, b] (open_time inclusive) sem velas dentro de [start, end)."""
    step = timedelta(seconds=timeframe.seconds)
    inside = [c.open_time for c in candles if start <= c.open_time < end]
    if not inside:
        return [(start, end - step)]
    gaps: list[tuple[datetime, datetime]] = []
    if inside[0] > start:
        gaps.append((start, inside[0] - step))
    gaps += [(a + step, b - step) for a, b in pairwise(inside) if b - a > step]
    if inside[-1] < end - step:
        gaps.append((inside[-1] + step, end - step))
    return gaps


def longest_contiguous(candles: Sequence[Candle], timeframe: Timeframe) -> tuple[list[Candle], int]:
    """Maior trecho sem buracos (o gateway exige continuidade) e quantos buracos foram encontrados."""
    step = timedelta(seconds=timeframe.seconds)
    best: list[Candle] = []
    current: list[Candle] = []
    holes = 0
    for candle in candles:
        if current and candle.open_time - current[-1].open_time != step:
            holes += 1
            current = []
        current.append(candle)
        if len(current) > len(best):
            best = current
    return list(best), holes


def _reachable(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=_DB_TIMEOUT):
            return True
    except OSError:
        return False


async def load_candles(
    symbol: Symbol,
    timeframe: Timeframe,
    start: datetime,
    end: datetime,
    *,
    cache_dir: Path,
    use_db: bool = True,
    refresh: bool = False,
) -> LoadedCandles:
    """Banco Timescale (se acessivel) + cache local; o que faltar vem da Binance publica e vai ao banco.

    Devolve apenas o maior trecho continuo do periodo, com os buracos relatados nas notas.
    """
    notes: list[str] = []
    sources: list[str] = []
    settings: Settings | None = None
    repository: CandleRepository | None = None
    db_engine = None
    base: list[Candle] = []
    if use_db:
        try:
            settings = load_settings()
            if not _reachable(settings.database_host, settings.database_port):
                raise ConnectionError("banco inacessivel")
            db_engine = create_engine(settings.database_url)
            repository = CandleRepository(create_session_factory(db_engine))
            from_db = await repository.fetch_range(symbol, timeframe, start, end)
            if from_db:
                base = merge_candles(base, from_db)
                sources.append(f"timescaledb ({len(from_db)} velas)")
        except Exception as error:  # o banco e opcional: qualquer falha cai para Binance/cache
            repository = None
            notes.append(f"banco indisponivel ({type(error).__name__}): usando cache local e Binance publica")
    path = cache_path(cache_dir, symbol, timeframe)
    cached = [] if refresh else read_cache(path, symbol, timeframe)
    if cached:
        base = merge_candles(base, cached)
        sources.append(f"cache local {path.name} ({len(cached)} velas)")
    try:
        gaps = missing_ranges(base, start, end, timeframe)
        fetched: list[Candle] = []
        if gaps:
            async with BinanceMarketData() as market:
                for gap_start, gap_end in gaps:
                    fetched += await market.fetch_candles(
                        symbol, timeframe, gap_start, gap_end + timedelta(seconds=1)
                    )
            sources.append(f"Binance publica ({len(fetched)} velas novas)")
        merged = merge_candles(base, fetched)
        if fetched:
            write_cache(path, merge_candles(cached, merged))
            if repository is not None:
                await repository.upsert_many(fetched)
                notes.append(f"{len(fetched)} velas persistidas no banco")
    finally:
        if db_engine is not None:
            await db_engine.dispose()
    window = [c for c in merged if start <= c.open_time < end]
    contiguous, holes = longest_contiguous(window, timeframe)
    if holes:
        notes.append(
            f"{holes} buraco(s) nos dados da Binance: usado o maior trecho continuo "
            f"({len(contiguous)} de {len(window)} velas)"
        )
    return LoadedCandles(contiguous, " + ".join(sources) or "nenhuma", tuple(notes))
