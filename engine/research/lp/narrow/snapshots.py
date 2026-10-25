from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from engine.research.lp.data.rpc import PUBLIC_RPC, PublicRpc, RpcError
from engine.research.lp.narrow.bars import PathData, build_path, regularize
from engine.research.lp.narrow.profile import Profile, effective_depth
from engine.research.lp.narrow.snapshot import (
    PoolSnapshot,
    build_profile,
    fetch_snapshot,
    snapshot_price_usd,
)

DEPTH_WIDTHS = (0.005, 0.01, 0.02, 0.05, 0.10)


@dataclass(frozen=True, slots=True)
class PoolData:
    meta: dict[str, Any]
    path: PathData
    snapshot: PoolSnapshot
    stable_is_token0: bool

    @property
    def pool_id(self) -> str:
        return str(self.meta["pool_id"])

    @property
    def fee(self) -> float:
        return float(self.meta["fee"])

    @property
    def category(self) -> str:
        return str(self.meta["category"])

    @property
    def profile(self) -> Profile:
        return build_profile(self.snapshot, self.stable_is_token0)

    @property
    def tvl_usd(self) -> float:
        return float(self.meta["reserve_usd"])


def stable_address(meta: dict[str, Any]) -> str:
    return str(meta["quote_address"] if meta["volatile_side"] == "base" else meta["base_address"])


def snapshot_dir(cache_dir: Path) -> Path:
    return cache_dir / "snapshots"


def fetch_all_snapshots(dataset: dict[str, Any], cache_dir: Path, force: bool = False) -> dict[str, str]:
    """Snapshot (somente leitura) de cada pool; devolve motivo de falha por pool_id."""
    failures: dict[str, str] = {}
    for meta in dataset["pools"]:
        url = PUBLIC_RPC.get(meta["network"])
        if url is None:
            failures[meta["pool_id"]] = "sem RPC publico configurado"
            continue
        out = snapshot_dir(cache_dir) / f"{meta['pool_id'].replace(':', '_')}.json"
        if out.exists() and not force:
            continue
        rpc = PublicRpc(url, cache_dir / "rpc")
        try:
            snap = fetch_snapshot(rpc, meta["address"], meta["base_address"], meta["quote_address"])
        except (RpcError, PermissionError, OSError, KeyError, ValueError) as exc:
            failures[meta["pool_id"]] = f"{type(exc).__name__}: {exc}"
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(snap.to_json()), encoding="utf-8")
    return failures


def load_pools(dataset: dict[str, Any], cache_dir: Path) -> tuple[list[PoolData], dict[str, str]]:
    pools: list[PoolData] = []
    missing: dict[str, str] = {}
    for meta in dataset["pools"]:
        f = snapshot_dir(cache_dir) / f"{meta['pool_id'].replace(':', '_')}.json"
        if not f.exists():
            missing[meta["pool_id"]] = "sem snapshot de ticks"
            continue
        snap = PoolSnapshot.from_json(json.loads(f.read_text(encoding="utf-8")))
        stable0 = snap.token0.lower() == stable_address(meta)
        bars = regularize(meta["rows"], dataset["window_start_ts"], dataset["window_end_ts"])
        pools.append(PoolData(meta, build_path(bars), snap, stable0))
    return pools, missing


def depth_table(pool: PoolData) -> dict[str, Any]:
    prof = pool.profile
    price = snapshot_price_usd(pool.snapshot, pool.stable_is_token0)
    rows = []
    for w in DEPTH_WIDTHS:
        mean_l, usd50 = effective_depth(prof, w)
        v2_l = pool.tvl_usd / (2 * price**0.5)
        rows.append(
            {
                "half_width": w,
                "usd_for_half_share": usd50,
                "kappa": mean_l / v2_l if v2_l > 0 else float("nan"),
                "share_of_tvl": usd50 / pool.tvl_usd if pool.tvl_usd > 0 else float("nan"),
            }
        )
    return {
        "pool_id": pool.pool_id,
        "block": pool.snapshot.block,
        "price_usd": price,
        "ticks_read": len(pool.snapshot.ticks),
        "active_depth_usd": prof.active_depth_usd(),
        "widths": rows,
    }
