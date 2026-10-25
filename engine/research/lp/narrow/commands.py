from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from engine.research.lp.narrow.calibrate import calibrate_all
from engine.research.lp.narrow.fetch import FetchParams, fetch_narrow_dataset
from engine.research.lp.narrow.runner import run_primary, run_sensitivity, write_json
from engine.research.lp.narrow.snapshots import fetch_all_snapshots, load_pools

CACHE_DIR = Path("data/cache/lp")
DATASET = CACHE_DIR / "narrow_dataset.json"
REPORT_DIR = Path("reports/lp")
PRIMARY = REPORT_DIR / "narrow_primary.json"
SENSITIVITY = REPORT_DIR / "narrow_sensitivity.json"
CALIBRATION = REPORT_DIR / "narrow_calibration.json"
REPORT = REPORT_DIR / "narrow_range_report.md"
# Pools do replay: o de maior liquidez BTC e ETH de cada rede (criterio ja usado na selecao).
CALIBRATION_CATEGORIES = {"btc", "eth"}


def load_dataset() -> dict[str, Any]:
    doc: dict[str, Any] = json.loads(DATASET.read_text(encoding="utf-8"))
    return doc


def cmd_fetch(ttl_hours: float) -> str:
    doc = fetch_narrow_dataset(FetchParams(CACHE_DIR, DATASET, ttl_hours))
    return f"{len(doc['pools'])} pools, {len(doc['skipped'])} descartados -> {DATASET}"


def cmd_snapshot() -> str:
    failures = fetch_all_snapshots(load_dataset(), CACHE_DIR)
    return f"snapshots em {CACHE_DIR / 'snapshots'}; falhas: {failures or 'nenhuma'}"


def cmd_calibrate() -> str:
    pools, _ = load_pools(load_dataset(), CACHE_DIR)
    wanted = {p.pool_id for p in pools if p.category in CALIBRATION_CATEGORIES}
    write_json(CALIBRATION, calibrate_all(pools, CACHE_DIR, wanted))
    return str(CALIBRATION)


def cmd_run(mode: str) -> str:
    pools, missing = load_pools(load_dataset(), CACHE_DIR)
    if mode == "primary":
        doc = run_primary(pools)
        doc["meta"]["missing_snapshots"] = missing
        write_json(PRIMARY, doc)
        return str(PRIMARY)
    write_json(SENSITIVITY, run_sensitivity(pools))
    return str(SENSITIVITY)
