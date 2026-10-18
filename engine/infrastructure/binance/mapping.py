from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from engine.domain.candle import Candle
from engine.domain.market import BookLevel, OrderBookSnapshot
from engine.domain.types import Symbol, Timeframe


def to_exchange_symbol(symbol: Symbol) -> str:
    return symbol.replace("/", "")


def stream_name(symbol: Symbol) -> str:
    return to_exchange_symbol(symbol).lower()


def from_millis(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1000, tz=UTC)


def to_millis(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def parse_rest_kline(
    symbol: Symbol, timeframe: Timeframe, row: Sequence[Any], server_time: datetime
) -> Candle:
    """Linha de /klines: [openTime, o, h, l, c, v, closeTime, ...] com valores como string.

    `closed` e decidido pelo horario do servidor, nunca pelo relogio local (evita lookahead).
    """
    open_time = from_millis(int(row[0]))
    close_time = from_millis(int(row[6]))
    return Candle(
        symbol=symbol,
        timeframe=timeframe,
        open_time=open_time,
        open=Decimal(row[1]),
        high=Decimal(row[2]),
        low=Decimal(row[3]),
        close=Decimal(row[4]),
        volume=Decimal(row[5]),
        closed=close_time < server_time,
    )


def parse_ws_kline(symbol: Symbol, timeframe: Timeframe, data: Mapping[str, Any]) -> Candle | None:
    """Retorna a vela somente quando a corretora a marca como fechada (`k.x`)."""
    kline = data.get("k")
    if data.get("e") != "kline" or kline is None or not kline["x"]:
        return None
    return Candle(
        symbol=symbol,
        timeframe=timeframe,
        open_time=from_millis(int(kline["t"])),
        open=Decimal(kline["o"]),
        high=Decimal(kline["h"]),
        low=Decimal(kline["l"]),
        close=Decimal(kline["c"]),
        volume=Decimal(kline["v"]),
        closed=True,
    )


def parse_depth(symbol: Symbol, data: Mapping[str, Any], received_at: datetime) -> OrderBookSnapshot:
    def levels(raw: Sequence[Sequence[str]]) -> tuple[BookLevel, ...]:
        return tuple(BookLevel(price=Decimal(p), quantity=Decimal(q)) for p, q in raw if Decimal(q) > 0)

    return OrderBookSnapshot(
        symbol=symbol, bids=levels(data["bids"]), asks=levels(data["asks"]), received_at=received_at
    )
