from __future__ import annotations

from collections.abc import Sequence
from math import exp, log
from typing import Any

from engine.research.lp.stats import median, quantile

Pool = dict[str, Any]
COMPONENTS = ("fees", "il", "directional", "gas", "swap", "excess")


def select(pools: Sequence[Pool], categories: set[str] | None = None) -> list[Pool]:
    return [p for p in pools if categories is None or p["category"] in categories]


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else float("nan")


def decomposition(pools: Sequence[Pool], key: str) -> dict[str, float]:
    """Media por componente (aditiva: soma = excesso medio) e medianas das metricas operacionais."""
    cells = [p["results"][key] for p in pools]
    out = {c: mean([r[c] for r in cells]) for c in COMPONENTS}
    out["excess_median"] = median([r["excess"] for r in cells])
    out["excess_p10"] = quantile([r["excess"] for r in cells], 0.10)
    out["excess_p90"] = quantile([r["excess"] for r in cells], 0.90)
    out["share_pos"] = sum(r["excess"] > 0 for r in cells) / len(cells) if cells else float("nan")
    out["per_day"] = median([r["per_day"] for r in cells])
    out["cost_per_rebalance_usd"] = median([r["cost_per_rebalance_usd"] for r in cells])
    out["time_in_range"] = median([r["time_in_range"] for r in cells])
    out["net_return_median"] = median([r["net_return"] for r in cells])
    out["hodl_return_median"] = median([r["hodl_return"] for r in cells])
    out["fees_median"] = median([r["fees"] for r in cells])
    return out


def cash_return(hours: float, rate_year: float) -> float:
    return float((1 + rate_year) ** (hours / 8760) - 1)


def lvr_ratio(pools: Sequence[Pool], key: str) -> float:
    """Mediana de (IL realizada simulada) / (LVR teorico); ambos negativos."""
    ratios = [
        p["results"][key]["il"] / p["results"][key]["lvr_theory"]
        for p in pools
        if p["results"][key]["lvr_theory"] < 0
    ]
    return median(ratios)


def calibration_summary(cal: dict[str, Any]) -> dict[str, Any]:
    """Razao real/modelo das taxas por largura (media geometrica) e razao de volume (replay/GeckoTerminal)."""
    ratios: dict[str, list[float]] = {}
    vols: list[float] = []
    n_windows = 0
    for pool in cal["pools"]:
        for w in pool["windows"]:
            if w["skipped"]:
                continue
            n_windows += 1
            vols.append(w["volume_events_usd"] / w["volume_gecko_usd"])
            for width, real in w["fees_real"].items():
                model = w["fees_model"][width]
                if real > 0 and model > 0:
                    ratios.setdefault(width, []).append(real / model)
    geo = {k: exp(mean([log(r) for r in v])) for k, v in ratios.items() if v}
    spread = {k: (quantile(v, 0.10), quantile(v, 0.90)) for k, v in ratios.items() if v}
    return {
        "n_windows": n_windows,
        "n_pools": len(cal["pools"]),
        "volume_ratio": median(vols),
        "geo": geo,
        "spread": spread,
        "failures": cal.get("failures", {}),
    }
