from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from math import log, sqrt
from pathlib import Path
from typing import Any

from engine.research.lp.gas import DEFAULT_GAS, cost_model
from engine.research.lp.narrow.profile import Profile
from engine.research.lp.narrow.sim import (
    GasUsd,
    Params,
    Result,
    Rule,
    block_excess,
    blocks,
    simulate_path,
)
from engine.research.lp.narrow.snapshot import build_profile, snapshot_price_usd
from engine.research.lp.narrow.snapshots import PoolData, depth_table
from engine.research.lp.stats import binom_two_sided, bonferroni_alpha, quantile

# Grade fixada ANTES de rodar; o primario roda uma unica vez. Sensibilidades ficam em outro arquivo.
WIDTHS = (0.0025, 0.005, 0.01, 0.02, 0.05, 0.10)
RULES = (
    Rule("fixed"),
    Rule("close"),
    Rule("pre", 0.7),
    Rule("pre", 0.8),
    Rule("pre", 0.9),
    Rule("pre", 0.8, 6.0),
    Rule("pre", 0.8, 24.0),
)
SENS_WIDTHS = (0.005, 0.01, 0.02)
SENS_RULES = (Rule("close"), Rule("pre", 0.8), Rule("pre", 0.8, 6.0))
BLOCK_HOURS = 720
CASH_RATE_YEAR = 0.045
GAS_SCALES = {"baixo": 0.4, "medio": 1.0, "alto": 5.0}
RegimeLabel = str


@dataclass(frozen=True, slots=True)
class Scenario:
    key: str
    label: str
    capital_usd: float = 1000.0
    gas_scale: float = 1.0
    swap_fee: float | None = None
    delay_min: float = 0.0
    competition: float = 0.0
    depth_mult: float = 1.0
    uniform_v2: bool = False


BASE = Scenario("base", "Caso base (gas medio, perfil real do snapshot, sem atraso, sem competicao extra)")
SENSITIVITIES = (
    Scenario("gas_baixo", "Gas L2 baixo (x0,4)", gas_scale=0.4),
    Scenario("gas_alto", "Gas L2 alto (x5)", gas_scale=5.0),
    Scenario("outros_x0.1", "Outros LPs com 1/10 da liquidez", depth_mult=0.1),
    Scenario("outros_x0.5", "Outros LPs com 1/2 da liquidez", depth_mult=0.5),
    Scenario("outros_x0.25", "Outros LPs com 1/4 da liquidez (menos competicao)", depth_mult=0.25),
    Scenario("outros_x4", "Outros LPs com 4x a liquidez (mais competicao)", depth_mult=4.0),
    Scenario("outros_uniformes", "Hipotese antiga: outros em faixa total (kappa=1)", uniform_v2=True),
    Scenario("swap_0.01%", "Rebalancear por rota barata (taxa de swap 0,01%)", swap_fee=0.0001),
    Scenario("swap_0.30%", "Rebalancear por rota cara (taxa de swap 0,30%)", swap_fee=0.003),
    Scenario("atraso_5m", "Reajuste executado 5 min apos o gatilho", delay_min=5.0),
    Scenario("atraso_15m", "Reajuste executado 15 min apos o gatilho", delay_min=15.0),
    Scenario("atraso_60m", "Reajuste executado 60 min apos o gatilho", delay_min=60.0),
    Scenario("competicao_25", "JIT/gestores ativos tiram 25% das taxas", competition=0.25),
    Scenario("competicao_50", "JIT/gestores ativos tiram 50% das taxas", competition=0.50),
    Scenario("capital_100", "Capital US$ 100", capital_usd=100.0),
    Scenario("capital_5000", "Capital US$ 5.000", capital_usd=5000.0),
    Scenario("capital_25000", "Capital US$ 25.000", capital_usd=25000.0),
)


def config_key(width: float, rule: Rule) -> str:
    return f"{width * 100:g}%|{rule.label}"


def gas_usd(chain: str, scale: float) -> GasUsd:
    cm = cost_model(DEFAULT_GAS[chain], gas_scale=scale)
    return GasUsd(cm.gas_open_usd, cm.gas_close_usd, cm.gas_swap_usd)


def scenario_profile(pool: PoolData, sc: Scenario) -> Profile:
    if sc.uniform_v2:
        return Profile.v2_equivalent(pool.tvl_usd, snapshot_price_usd(pool.snapshot, pool.stable_is_token0))
    return build_profile(pool.snapshot, pool.stable_is_token0, sc.depth_mult)


def run_one(pool: PoolData, profile: Profile, sc: Scenario, width: float, rule: Rule) -> Result:
    params = Params(
        capital_usd=sc.capital_usd,
        half_width=width,
        rule=rule,
        fee=pool.fee,
        gas=gas_usd(str(pool.meta["chain"]), sc.gas_scale),
        swap_fee=pool.fee if sc.swap_fee is None else sc.swap_fee,
        delay_min=sc.delay_min,
        competition=sc.competition,
    )
    return simulate_path(pool.path, profile, params)


def compact(res: Result) -> dict[str, Any]:
    cap = res.capital_usd
    return {
        "fees": res.fees_usd / cap,
        "il": res.il_usd / cap,
        "gas": -res.gas_usd / cap,
        "swap": -res.swap_usd / cap,
        "directional": res.directional_usd / cap,
        "excess": res.excess_pct,
        "hodl_return": res.hodl_usd / cap - 1,
        "net_return": res.net_usd / cap - 1,
        "rebalances": res.rebalances,
        "per_day": res.per_day,
        "cost_per_rebalance_usd": (res.gas_usd + res.swap_usd) / max(res.rebalances, 1),
        "time_in_range": res.time_in_range,
        "lvr_theory": -res.lvr_theory_usd / cap,
        "blocks": [block_excess(res, s, e) for s, e in blocks(res.hours, BLOCK_HOURS)],
    }


def summarize(values: Sequence[float]) -> dict[str, float]:
    return {
        "median": quantile(values, 0.5),
        "p10": quantile(values, 0.10),
        "p90": quantile(values, 0.90),
        "share_pos": sum(v > 0 for v in values) / len(values) if values else float("nan"),
        "n": float(len(values)),
    }


def pool_regimes(pool: PoolData) -> list[dict[str, Any]]:
    closes = [b.close for b in pool.path.bars]
    out: list[dict[str, Any]] = []
    sigmas: list[float] = []
    spans = blocks(len(closes), BLOCK_HOURS)
    for s, e in spans:
        rets = [log(closes[i] / closes[i - 1]) for i in range(max(s, 1), e)]
        mean = sum(rets) / len(rets)
        sigmas.append(sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)))
    med_sigma = quantile(sigmas, 0.5)
    for (s, e), sg in zip(spans, sigmas, strict=True):
        change = closes[e - 1] / (closes[s - 1] if s > 0 else pool.path.bars[0].open) - 1
        label = "alta" if change > 0.10 else "baixa" if change < -0.10 else "lateral"
        out.append({"change": change, "regime": label, "high_vol": sg >= med_sigma, "sigma_hour": sg})
    return out


def run_primary(pools: Sequence[PoolData]) -> dict[str, Any]:
    out_pools: list[dict[str, Any]] = []
    cells: dict[str, list[float]] = {}
    by_regime: dict[str, dict[str, list[float]]] = {}
    for pool in pools:
        profile = scenario_profile(pool, BASE)
        regimes = pool_regimes(pool)
        results: dict[str, Any] = {}
        for w in WIDTHS:
            for rule in RULES:
                res = run_one(pool, profile, BASE, w, rule)
                key = config_key(w, rule)
                results[key] = compact(res)
                cells.setdefault(key, []).append(res.excess_pct)
                for blk, reg in zip(results[key]["blocks"], regimes, strict=False):
                    for tag in (reg["regime"], "vol_alta" if reg["high_vol"] else "vol_baixa"):
                        by_regime.setdefault(key, {}).setdefault(tag, []).append(blk)
        out_pools.append(
            {
                "pool_id": pool.pool_id,
                "name": pool.meta["name"],
                "chain": pool.meta["chain"],
                "category": pool.category,
                "fee": pool.fee,
                "tvl_usd": pool.tvl_usd,
                "hours": len(pool.path.bars),
                "sigma_year": sqrt(pool.path.sigma2_hour * 8760),
                "depth": depth_table(pool),
                "regimes": regimes,
                "results": results,
            }
        )
    n_tests = len(WIDTHS) * len(RULES)
    alpha = bonferroni_alpha(0.05, n_tests)
    summary: dict[str, Any] = {}
    for key, vals in cells.items():
        pos = sum(v > 0 for v in vals)
        summary[key] = {
            **summarize(vals),
            "p_binomial": binom_two_sided(pos, len(vals)),
            "significant_bonferroni": binom_two_sided(pos, len(vals)) < alpha,
            "regimes": {tag: summarize(v) for tag, v in by_regime.get(key, {}).items()},
        }
    return {
        "meta": {
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "scenario": BASE.label,
            "capital_usd": BASE.capital_usd,
            "widths": list(WIDTHS),
            "rules": [r.label for r in RULES],
            "n_pools": len(pools),
            "n_tests": n_tests,
            "bonferroni_alpha": alpha,
            "cash_rate_year": CASH_RATE_YEAR,
            "block_hours": BLOCK_HOURS,
        },
        "pools": out_pools,
        "summary": summary,
    }


def run_sensitivity(pools: Sequence[PoolData]) -> dict[str, Any]:
    out: dict[str, Any] = {"scenarios": {}, "meta": {"widths": list(SENS_WIDTHS)}}
    for sc in (BASE, *SENSITIVITIES):
        cells: dict[str, dict[str, list[float]]] = {}
        for pool in pools:
            profile = scenario_profile(pool, sc)
            for w in SENS_WIDTHS:
                for rule in SENS_RULES:
                    res = run_one(pool, profile, sc, w, rule)
                    cell = cells.setdefault(config_key(w, rule), {"excess": [], "per_day": [], "fees": []})
                    cell["excess"].append(res.excess_pct)
                    cell["per_day"].append(res.per_day)
                    cell["fees"].append(res.fees_usd / res.capital_usd)
        out["scenarios"][sc.key] = {
            "label": sc.label,
            "cells": {
                k: {
                    **summarize(v["excess"]),
                    "per_day": quantile(v["per_day"], 0.5),
                    "fees": quantile(v["fees"], 0.5),
                }
                for k, v in cells.items()
            },
        }
    return out


def write_json(path: Path, doc: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=1), encoding="utf-8")
