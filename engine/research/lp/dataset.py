from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import date, timedelta
from itertools import pairwise
from typing import Any

from engine.research.lp.data.llama import LlamaPool, PoolDay
from engine.research.lp.types import Category, Day, PoolSeries

STABLES = frozenset({"USDC", "USDT", "DAI", "USDC.E", "USDT0", "USDBC", "USDS"})
MAJORS = frozenset({"WETH", "ETH", "WBTC", "CBBTC"})
MIDCAPS = frozenset({"LINK", "UNI", "AAVE", "ARB", "OP", "POL", "WPOL", "WMATIC", "MATIC", "LDO"})
MAX_TVL_GAP_DAYS = 3
MAX_FILL_DAYS = 7


def classify(symbol: str) -> Category | None:
    parts = [p.upper() for p in symbol.split("-")]
    if len(parts) != 2 or parts[0] == parts[1]:
        return None
    a, b = parts
    safe = STABLES | MAJORS
    if a in STABLES and b in STABLES:
        return "estavel"
    if a in safe and b in safe:
        return "volatil_maior"
    if (a in MIDCAPS and b in safe) or (b in MIDCAPS and a in safe):
        return "volatil_media"
    return None


def candidates(pools: Sequence[LlamaPool], chain: str, limit: int, stable_limit: int = 8) -> list[LlamaPool]:
    """Candidatos objetivos: whitelist, taxa conhecida, maior TVL atual (estaveis em lista propria)."""
    ok = [
        p
        for p in pools
        if p.chain == chain and p.fee is not None and len(p.tokens) == 2 and classify(p.symbol)
    ]
    ranked = sorted(ok, key=lambda p: -p.tvl_usd)
    stables = [p for p in ranked if classify(p.symbol) == "estavel"][:stable_limit]
    others = [p for p in ranked if classify(p.symbol) != "estavel"][:limit]
    return stables + others


def tvl_at(history: dict[date, PoolDay], target: date) -> float | None:
    for gap in range(MAX_TVL_GAP_DAYS + 1):
        value = history.get(target + timedelta(days=gap))
        if value is not None:
            return value.tvl_usd
    return None


def rank_by_start_tvl(
    items: Sequence[tuple[LlamaPool, dict[date, PoolDay]]], start: date, min_tvl: float
) -> list[tuple[LlamaPool, float]]:
    """Ordena pelo TVL na data inicial (sem olhar retorno); descarta pools inexistentes/pequenos no inicio."""
    ranked = [(p, v) for p, h in items if (v := tvl_at(h, start)) is not None and v >= min_tvl]
    return sorted(ranked, key=lambda t: -t[1])


def build_days(
    base_usd: dict[date, float],
    quote_usd: dict[date, float],
    history: dict[date, PoolDay],
    fee: float,
    start: date,
    end: date,
) -> list[Day]:
    """Serie diaria: preco = USD(base)/USD(quote); volume implicito = apyBase/365 x TVL / taxa.

    Sem OHLC: low/high de cada dia vem do fechamento anterior e do atual (aproximacao).
    Dias sem dado do pool repetem o ultimo TVL/volume observado, por no maximo MAX_FILL_DAYS dias.
    """
    out: list[Day] = []
    prev_close: float | None = None
    last: tuple[date, float, float] | None = None
    day = start
    while day <= end:
        h = history.get(day)
        if h is not None and h.apy_base is not None and h.tvl_usd > 0 and fee > 0:
            last = (day, h.tvl_usd, h.apy_base / 100 / 365 * h.tvl_usd / fee)
        usable = last is not None and (day - last[0]).days <= MAX_FILL_DAYS
        b, q = base_usd.get(day), quote_usd.get(day)
        if b and q and usable and last is not None:
            close = b / q
            ref = prev_close if prev_close is not None else close
            out.append(
                Day(
                    day=day,
                    low=min(ref, close),
                    high=max(ref, close),
                    close=close,
                    quote_usd=q,
                    volume_usd=last[2],
                    tvl_usd=last[1],
                )
            )
            prev_close = close
        day += timedelta(days=1)
    return out


def is_contiguous(days: Sequence[Day], max_gap: int = 2) -> bool:
    return all((b.day - a.day).days <= max_gap for a, b in pairwise(days))


def series_hash(days: Sequence[Day]) -> str:
    payload = json.dumps(
        [[d.day.isoformat(), d.low, d.high, d.close, d.quote_usd, d.volume_usd, d.tvl_usd] for d in days]
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def pool_to_json(p: PoolSeries) -> dict[str, Any]:
    return {
        "pool_id": p.pool_id,
        "chain": p.chain,
        "address": p.address,
        "name": p.name,
        "fee": p.fee,
        "category": p.category,
        "tvl_start_usd": p.tvl_start_usd,
        "tvl_end_usd": p.tvl_end_usd,
        "sha256": series_hash(p.days),
        "days": [
            [d.day.isoformat(), d.low, d.high, d.close, d.quote_usd, d.volume_usd, d.tvl_usd] for d in p.days
        ],
    }


def pool_from_json(doc: dict[str, Any]) -> PoolSeries:
    days = tuple(Day(date.fromisoformat(r[0]), r[1], r[2], r[3], r[4], r[5], r[6]) for r in doc["days"])
    return PoolSeries(
        pool_id=doc["pool_id"],
        chain=doc["chain"],
        address=doc["address"],
        name=doc["name"],
        fee=doc["fee"],
        category=doc["category"],
        days=days,
        tvl_start_usd=doc["tvl_start_usd"],
        tvl_end_usd=doc["tvl_end_usd"],
    )
