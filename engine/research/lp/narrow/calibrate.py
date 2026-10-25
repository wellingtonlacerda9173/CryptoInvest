from __future__ import annotations

from pathlib import Path
from typing import Any

from engine.research.lp.data.rpc import LOG_CHUNK_BLOCKS, PUBLIC_RPC, PublicRpc, RpcError
from engine.research.lp.narrow.bars import HOUR, build_path, regularize
from engine.research.lp.narrow.replay import replay_fees, swaps_in_window
from engine.research.lp.narrow.sim import GasUsd, Params, Rule, simulate_path
from engine.research.lp.narrow.snapshots import PoolData

CAL_WIDTHS = (0.0025, 0.005, 0.01, 0.02, 0.05)
CAL_CAPITAL = 1000.0
CAL_DAYS = 3


def calibrate_pool(pool: PoolData, cache_dir: Path, days: int = CAL_DAYS) -> dict[str, Any]:
    """Compara taxas REAIS (replay de eventos Swap) com as do modelo horario, em janelas de 24h passadas."""
    url = PUBLIC_RPC[str(pool.meta["network"])]
    rpc = PublicRpc(url, cache_dir / "rpc")
    bars_all = regularize(pool.meta["rows"])
    end_ts = bars_all[-1].ts + HOUR
    windows: list[dict[str, Any]] = []
    chunk = LOG_CHUNK_BLOCKS[str(pool.meta["network"])]
    for d in range(days):
        w_end = end_ts - d * 24 * HOUR
        w_start = w_end - 24 * HOUR
        events = swaps_in_window(rpc, str(pool.meta["address"]), w_start, w_end, chunk)
        bars = [b for b in bars_all if w_start <= b.ts < w_end]
        if len(events) < 50 or len(bars) < 20:
            windows.append({"start_ts": w_start, "swaps": len(events), "bars": len(bars), "skipped": True})
            continue
        real = replay_fees(events, pool.snapshot, pool.stable_is_token0, pool.fee, CAL_WIDTHS, CAL_CAPITAL)
        path = build_path(bars)
        model_fees: dict[float, float] = {}
        for w in CAL_WIDTHS:
            params = Params(CAL_CAPITAL, w, Rule("fixed"), pool.fee, GasUsd(0, 0, 0), pool.fee)
            model_fees[w] = simulate_path(path, pool.profile, params).fees_usd
        windows.append(
            {
                "start_ts": w_start,
                "swaps": real.swaps,
                "volume_events_usd": real.volume_usd,
                "volume_gecko_usd": sum(b.volume_usd for b in bars),
                "fees_real": {str(w): v for w, v in real.fees_usd.items()},
                "fees_model": {str(w): v for w, v in model_fees.items()},
                "skipped": False,
            }
        )
    return {"pool_id": pool.pool_id, "name": pool.meta["name"], "windows": windows}


def calibrate_all(pools: list[PoolData], cache_dir: Path, wanted: set[str]) -> dict[str, Any]:
    out: list[dict[str, Any]] = []
    failures: dict[str, str] = {}
    for pool in pools:
        if pool.pool_id not in wanted:
            continue
        try:
            out.append(calibrate_pool(pool, cache_dir))
        except (RpcError, OSError, KeyError, ValueError, ZeroDivisionError) as exc:
            failures[pool.pool_id] = f"{type(exc).__name__}: {exc}"
    return {"widths": list(CAL_WIDTHS), "capital_usd": CAL_CAPITAL, "pools": out, "failures": failures}
