from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from engine.research.lp.data.coins import CoinsClient
from engine.research.lp.data.http import CachedGetClient
from engine.research.lp.data.llama import LlamaClient
from engine.research.lp.dataset import (
    build_days,
    candidates,
    classify,
    is_contiguous,
    pool_to_json,
    rank_by_start_tvl,
    tvl_at,
)
from engine.research.lp.types import PoolSeries

HISTORY_DAYS = 545
PER_CHAIN = {"Ethereum": 14, "Arbitrum": 6, "Base": 6, "OP Mainnet": 4, "Polygon": 4}
CANDIDATES_PER_CHAIN = 30
MIN_START_TVL = 1_000_000.0
MIN_DAYS = 520
STABLE_PER_CHAIN = 2


@dataclass(frozen=True, slots=True)
class FetchParams:
    cache_dir: Path
    out_path: Path
    ttl_hours: float = 24.0
    per_chain: dict[str, int] | None = None


def fetch_dataset(params: FetchParams) -> dict[str, Any]:
    http = CachedGetClient(params.cache_dir, ttl_hours=params.ttl_hours)
    llama, coins = LlamaClient(http), CoinsClient(http)
    end = datetime.now(UTC).date() - timedelta(days=1)
    start = end - timedelta(days=HISTORY_DAYS)
    all_pools = llama.pools()
    selected: list[PoolSeries] = []
    skipped: list[dict[str, Any]] = []
    for chain, quota in (params.per_chain or PER_CHAIN).items():
        cands = candidates(all_pools, chain, CANDIDATES_PER_CHAIN)
        histories = [(p, llama.history(p.pool_id)) for p in cands]
        too_new = [p.pool_id for p, h in histories if tvl_at(h, start) is None]
        skipped += [
            {"pool_id": i, "chain": chain, "motivo": "sem TVL na data inicial (pool novo)"} for i in too_new
        ]
        taken = {"estavel": 0, "outros": 0}
        history_of = {p.pool_id: h for p, h in histories}
        for pool, tvl_start in rank_by_start_tvl(histories, start, MIN_START_TVL):
            bucket = "estavel" if classify(pool.symbol) == "estavel" else "outros"
            if taken[bucket] >= (STABLE_PER_CHAIN if bucket == "estavel" else quota):
                continue
            base, quote = pool.tokens
            fee = pool.fee or 0.0
            days = build_days(
                coins.prices(chain, base, start, end),
                coins.prices(chain, quote, start, end),
                history_of[pool.pool_id],
                fee,
                start,
                end,
            )
            category = classify(pool.symbol)
            if category is None or len(days) < MIN_DAYS or not is_contiguous(days, 12):
                skipped.append(
                    {
                        "pool_id": pool.pool_id,
                        "chain": chain,
                        "motivo": f"historico insuficiente ({len(days)} dias)",
                    }
                )
                continue
            selected.append(
                PoolSeries(
                    pool_id=pool.pool_id,
                    chain=chain,
                    address=pool.pool_id,
                    name=f"{pool.symbol} {fee * 100:g}%",
                    fee=fee,
                    category=category,
                    days=tuple(days),
                    tvl_start_usd=tvl_start,
                    tvl_end_usd=days[-1].tvl_usd,
                )
            )
            taken[bucket] += 1
    dataset = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "sources": {
            "defillama_yields": "https://yields.llama.fi (pools, chart/{pool}): TVL e apyBase diarios",
            "defillama_coins": "https://coins.llama.fi/chart/{chain}:{token} (preco diario USD)",
        },
        "requests": [
            {"url": f.url, "fetched_at": f.fetched_at, "sha256": f.sha256, "from_cache": f.from_cache}
            for f in http.log
        ],
        "skipped": skipped,
        "pools": [pool_to_json(p) for p in selected],
    }
    params.out_path.parent.mkdir(parents=True, exist_ok=True)
    params.out_path.write_text(json.dumps(dataset), encoding="utf-8")
    return dataset
