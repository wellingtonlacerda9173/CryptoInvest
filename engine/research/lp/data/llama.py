from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from engine.research.lp.data.http import CachedGetClient

POOLS_URL = "https://yields.llama.fi/pools"
CHART_URL = "https://yields.llama.fi/chart/{pool}"


@dataclass(frozen=True, slots=True)
class LlamaPool:
    pool_id: str
    chain: str
    symbol: str
    fee: float | None
    tvl_usd: float
    tokens: tuple[str, ...]


def parse_fee(meta: str | None) -> float | None:
    if not meta or not meta.rstrip().endswith("%"):
        return None
    try:
        return float(meta.strip().rstrip("%")) / 100
    except ValueError:
        return None


def parse_pools(body: dict[str, Any], project: str = "uniswap-v3") -> list[LlamaPool]:
    return [
        LlamaPool(
            pool_id=p["pool"],
            chain=p["chain"],
            symbol=p["symbol"],
            fee=parse_fee(p.get("poolMeta")),
            tvl_usd=float(p["tvlUsd"]),
            tokens=tuple(t.lower() for t in (p.get("underlyingTokens") or [])),
        )
        for p in body["data"]
        if p["project"] == project
    ]


@dataclass(frozen=True, slots=True)
class PoolDay:
    tvl_usd: float
    apy_base: float | None


def parse_history(body: dict[str, Any]) -> dict[date, PoolDay]:
    out: dict[date, PoolDay] = {}
    for row in body["data"]:
        ts = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")).astimezone(UTC)
        if row.get("tvlUsd") is not None:
            apy = row.get("apyBase")
            out[ts.date()] = PoolDay(float(row["tvlUsd"]), float(apy) if apy is not None else None)
    return out


class LlamaClient:
    def __init__(self, http: CachedGetClient) -> None:
        self._http = http

    def pools(self) -> list[LlamaPool]:
        return parse_pools(self._http.get_json(POOLS_URL).body)

    def history(self, pool_id: str) -> dict[date, PoolDay]:
        return parse_history(self._http.get_json(CHART_URL.format(pool=pool_id)).body)
