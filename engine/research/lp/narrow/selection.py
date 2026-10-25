from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Literal

from engine.research.lp.data.gecko import PoolInfo

NarrowCategory = Literal["btc", "eth", "media"]
STABLES = frozenset({"USDC", "USDT", "DAI", "USDC.E", "USDBC", "USDT0", "FRAX", "LUSD", "USDS", "USD₮0"})
BTC = frozenset({"WBTC", "CBBTC", "TBTC", "WBTC.E", "BTC.B", "LBTC"})
ETH = frozenset({"WETH", "ETH"})
EXCLUDED = frozenset({"WSTETH", "WEETH", "RETH", "EZETH", "STETH", "CBETH", "RSETH", "SWETH", "OSETH"})
MIN_AGE_DAYS = 190
MIN_COVERAGE = 0.7


def classify_pair(pool: PoolInfo) -> tuple[NarrowCategory, Literal["base", "quote"], str] | None:
    """(categoria, lado volatil do OHLCV, simbolo volatil) para pares estavel x volatil; senao None."""
    left, right = (s.upper() for s in pool.symbols)
    stable_left, stable_right = left in STABLES, right in STABLES
    if stable_left == stable_right:
        return None
    side: Literal["base", "quote"] = "quote" if stable_left else "base"
    volatile = right if stable_left else left
    if volatile in EXCLUDED:
        return None
    if volatile in BTC:
        return "btc", side, volatile
    if volatile in ETH:
        return "eth", side, volatile
    return "media", side, volatile


def old_enough(pool: PoolInfo, now: datetime | None = None) -> bool:
    if not pool.created_at:
        return False
    created = datetime.fromisoformat(pool.created_at.replace("Z", "+00:00"))
    return ((now or datetime.now(UTC)) - created).days >= MIN_AGE_DAYS


def ranked_candidates(
    pools: Iterable[PoolInfo], now: datetime | None = None
) -> dict[NarrowCategory, list[tuple[PoolInfo, Literal["base", "quote"], str]]]:
    """Criterio fixo e objetivo: BTC/ETH por liquidez (reserve_in_usd); moedas medias por volume 24h."""
    out: dict[NarrowCategory, list[tuple[PoolInfo, Literal["base", "quote"], str]]] = {
        "btc": [],
        "eth": [],
        "media": [],
    }
    for pool in pools:
        cls = classify_pair(pool)
        if cls is not None and old_enough(pool, now):
            out[cls[0]].append((pool, cls[1], cls[2]))
    for cat, lst in out.items():
        # BTC/ETH: liquidez. Medias: volume 24h (o ranking por liquidez escolheu pools inativos; ajuste feito
        # ao inspecionar os NOMES/volumes dos candidatos, antes de qualquer simulacao).
        lst.sort(key=lambda t: t[0].volume24h_usd if cat == "media" else t[0].reserve_usd, reverse=True)
    return out
