from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from engine.research.lp.data.gecko import NETWORK_DEXES, NETWORK_LABEL, GeckoClient
from engine.research.lp.data.http import CachedGetClient
from engine.research.lp.narrow.selection import MIN_COVERAGE, ranked_candidates

HISTORY_HOURS = 180 * 24
MAX_TRIES = 3
PER_CATEGORY = {"btc": 1, "eth": 1, "media": 1}


@dataclass(frozen=True, slots=True)
class FetchParams:
    cache_dir: Path
    out_path: Path
    ttl_hours: float = 24.0
    networks: tuple[str, ...] = tuple(NETWORK_DEXES)


def series_hash(rows: list[list[float]]) -> str:
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()


def fetch_narrow_dataset(params: FetchParams) -> dict[str, Any]:
    http = CachedGetClient(params.cache_dir, ttl_hours=params.ttl_hours, min_interval_s=2.2)
    gecko = GeckoClient(http)
    pools: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    window_end = int(time.time()) // 3600 * 3600 - 3600
    window_start = window_end - (HISTORY_HOURS - 1) * 3600
    for network in params.networks:
        try:
            listing = gecko.top_pools(network)
        except RuntimeError as exc:
            skipped.append({"rede": network, "motivo": f"listagem falhou: {exc}"})
            continue
        for category, cands in ranked_candidates(listing).items():
            taken = 0
            for pool, side, symbol in cands[:MAX_TRIES]:
                try:
                    rows = gecko.ohlcv_hour(network, pool.address, side, HISTORY_HOURS)
                except RuntimeError as exc:
                    skipped.append({"pool": pool.address, "rede": network, "motivo": str(exc)})
                    continue
                rows = [r for r in rows if window_start <= r[0] <= window_end]
                coverage = len(rows) / HISTORY_HOURS
                if coverage < MIN_COVERAGE:
                    skipped.append(
                        {
                            "pool": pool.address,
                            "rede": network,
                            "motivo": f"cobertura horaria {coverage:.0%} < {MIN_COVERAGE:.0%}",
                        }
                    )
                    continue
                pools.append(
                    {
                        "pool_id": f"{network}:{pool.address[:8]}",
                        "network": network,
                        "chain": NETWORK_LABEL[network],
                        "address": pool.address,
                        "name": pool.name,
                        "category": category,
                        "fee": pool.fee,
                        "reserve_usd": pool.reserve_usd,
                        "volume24h_usd": pool.volume24h_usd,
                        "volatile_symbol": symbol,
                        "volatile_side": side,
                        "base_address": pool.base_address,
                        "quote_address": pool.quote_address,
                        "created_at": pool.created_at,
                        "coverage": coverage,
                        "sha256": series_hash(rows),
                        "rows": rows,
                    }
                )
                taken += 1
                if taken >= PER_CATEGORY[category]:
                    break
    doc = {
        "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "source": "GeckoTerminal API publica v2 (sem chave), OHLCV horario, volume em USD",
        "history_hours": HISTORY_HOURS,
        "window_start_ts": window_start,
        "window_end_ts": window_end,
        "criterion": "BTC/ETH: maior reserve_in_usd; media: maior volume 24h (na coleta); idade >= 190 dias",
        "pools": pools,
        "skipped": skipped,
        "queries": [{"url": f.url, "fetched_at": f.fetched_at, "sha256": f.sha256} for f in http.log],
    }
    params.out_path.parent.mkdir(parents=True, exist_ok=True)
    params.out_path.write_text(json.dumps(doc), encoding="utf-8")
    return doc
