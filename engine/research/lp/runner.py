from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from engine.research.lp.dataset import pool_from_json
from engine.research.lp.gas import DEFAULT_GAS, cost_model
from engine.research.lp.simulate import apply_depeg, simulate
from engine.research.lp.types import Day, PoolSeries, Strategy

CAPITALS = (1_000.0, 5_000.0, 25_000.0)
WINDOW_LENGTHS = (90, 180, 365)
VOL_WIDTH = 0.10
STABLE_WIDTH = 0.005
REGIME_LIMIT = 0.20
L1 = "Ethereum"

# Choques de de-peg do token base (multiplicadores diarios do preco). Cenarios, nao previsoes.
STRESS = {
    "usdc_mar2023": ([0.97, 0.90, 0.88, 0.93, 0.98, 0.995, 1.0], 8.0),
    "ust_colapso": ([0.98, 0.92, 0.75, 0.50, 0.30, 0.20, 0.12, 0.08], 10.0),
}


@dataclass(frozen=True, slots=True)
class Scenario:
    label: str
    rho: float = 0.5
    gas_scale: float = 1.0
    swap_bps: float = 10.0
    vol_width: float = VOL_WIDTH
    stable_width: float = STABLE_WIDTH
    v2_kappa: float = 1.0


PRIMARY = Scenario("primario")
SENSITIVITY = (
    Scenario("rho=0 (ninguem mais concentrado)", rho=0.0),
    Scenario("rho=0.25", rho=0.25),
    Scenario("rho=0.75", rho=0.75),
    Scenario("faixa total em pool v3 kappa=5", v2_kappa=5.0),
    Scenario("faixa total em pool v3 kappa=20", v2_kappa=20.0),
    Scenario("rho=1 (concorrentes igualmente concentrados)", rho=1.0),
    Scenario("gas x0.2", gas_scale=0.2),
    Scenario("gas x5", gas_scale=5.0),
    Scenario("swap 3 bps", swap_bps=3.0),
    Scenario("swap 30 bps", swap_bps=30.0),
    Scenario("faixa volatil +-5%", vol_width=0.05),
    Scenario("faixa volatil +-25%", vol_width=0.25),
    Scenario("faixa estavel +-0.1%", stable_width=0.001),
    Scenario("faixa estavel +-2%", stable_width=0.02),
)


def windows(end: date, available_start: date) -> list[tuple[int, int, date, date]]:
    """(comprimento, indice, inicio, fim) ancorados no fim comum; blocos sem sobreposicao por comprimento."""
    out: list[tuple[int, int, date, date]] = []
    for length in WINDOW_LENGTHS:
        k = 0
        while True:
            w_end = end - timedelta(days=k * length)
            w_start = w_end - timedelta(days=length)
            if w_start < available_start:
                break
            out.append((length, k, w_start, w_end))
            k += 1
    return out


def regime(days: list[Day], category: str) -> str:
    if category == "estavel":
        return "estavel"
    change = days[-1].close * days[-1].quote_usd / (days[0].close * days[0].quote_usd) - 1
    if change > REGIME_LIMIT:
        return "alta"
    return "baixa" if change < -REGIME_LIMIT else "lateral"


def strategies(category: str, sc: Scenario) -> list[Strategy]:
    width = sc.stable_width if category == "estavel" else sc.vol_width
    return [
        Strategy("v2", v2_kappa=sc.v2_kappa),
        Strategy("v3_fixed", width, sc.rho),
        Strategy("v3_rebal", width, sc.rho),
    ]


def _row(
    pool: PoolSeries, sc: Scenario, st: Strategy, cap: float, win: tuple[Any, ...], reg: str, days: list[Day]
) -> dict[str, Any]:
    cm = cost_model(DEFAULT_GAS[pool.chain], sc.swap_bps, sc.gas_scale)
    r = simulate(days, st, cap, pool.fee, cm)
    return {
        "scenario": sc.label,
        "pool": pool.name,
        "pool_id": pool.pool_id,
        "chain": pool.chain,
        "category": pool.category,
        "tvl_ratio": pool.tvl_ratio,
        "window_len": win[0],
        "window_idx": win[1],
        "window_start": win[2].isoformat(),
        "regime": reg,
        "strategy": st.kind,
        "label": st.label,
        "capital": cap,
        "excess": r.excess_pct,
        "price_effect": r.price_effect_pct,
        "fees": r.fees_usd / cap,
        "gas": r.gas_usd / cap,
        "swap": r.swap_usd / cap,
        "rebalances": r.rebalances,
        "time_in_range": r.time_in_range,
    }


def run_scenarios(pools: list[PoolSeries], scenarios: tuple[Scenario, ...]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    end = max(p.days[-1].day for p in pools)
    for pool in pools:
        by_day = {d.day: d for d in pool.days}
        for win in windows(end, pool.days[0].day):
            days = [by_day[x] for x in sorted(by_day) if win[2] <= x <= win[3]]
            if len(days) < win[0] * 0.9:
                continue
            reg = regime(days, pool.category)
            for sc in scenarios:
                for st in strategies(pool.category, sc):
                    rows.extend(_row(pool, sc, st, cap, win, reg, days) for cap in CAPITALS)
    return rows


def run_stress(pools: list[PoolSeries]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for pool in (p for p in pools if p.category == "estavel"):
        base = list(pool.days[-181:])
        win = (180, 0, base[0].day, base[-1].day)
        for st in strategies("estavel", PRIMARY):
            calm = _row(pool, PRIMARY, st, 5_000.0, win, "estresse", base)
            calm["scenario"] = "calmo"
            rows.append(calm)
        for name, (mult, vol_mult) in STRESS.items():
            shocked = apply_depeg(base, 60, mult, vol_mult)
            win = (180, 0, base[0].day, base[-1].day)
            for st in strategies("estavel", PRIMARY):
                row = _row(pool, PRIMARY, st, 5_000.0, win, "estresse", shocked)
                row["scenario"] = name
                rows.append(row)
    return rows


def run_mode(dataset_path: Path, out_dir: Path, mode: str) -> str:
    ds = json.loads(dataset_path.read_text(encoding="utf-8"))
    pools = [pool_from_json(p) for p in ds["pools"]]
    if mode == "primary":
        rows = run_scenarios(pools, (PRIMARY,))
    elif mode == "sensitivity":
        rows = run_scenarios(pools, SENSITIVITY)
    else:
        rows = run_stress(pools)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"results_{mode}.json"
    path.write_text(json.dumps(rows), encoding="utf-8")
    return f"{len(rows)} linhas -> {path}"
