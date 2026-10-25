from __future__ import annotations

import json
import math
from datetime import date, timedelta
from pathlib import Path

import pytest

from engine.research.lp import runner
from engine.research.lp.dataset import pool_to_json
from engine.research.lp.report import write_report
from engine.research.lp.stats import (
    binom_two_sided,
    bonferroni_alpha,
    bootstrap_median_ci,
    median,
    quantile,
    share_positive,
)
from engine.research.lp.types import Day, PoolSeries


def test_estatisticas_basicas() -> None:
    assert median([3.0, 1.0, 2.0]) == 2.0
    assert quantile([0.0, 10.0], 0.25) == pytest.approx(2.5)
    assert share_positive([1.0, -1.0, 2.0, 0.0]) == 0.5
    assert math.isnan(median([]))
    assert bonferroni_alpha(0.05, 10) == pytest.approx(0.005)


def test_binomial_exato() -> None:
    assert binom_two_sided(5, 10) == pytest.approx(1.0)
    assert binom_two_sided(10, 10) == pytest.approx(2 / 1024)
    assert binom_two_sided(0, 0) == 1.0


def test_bootstrap_reprodutivel_e_contem_a_mediana() -> None:
    vals = [float(i) for i in range(1, 21)]
    lo, hi = bootstrap_median_ci(vals)
    assert (lo, hi) == bootstrap_median_ci(vals)
    assert lo <= median(vals) <= hi


def _pool(name: str, chain: str, category: str, drift: float, n: int = 560) -> PoolSeries:
    start = date(2025, 1, 1)
    days = tuple(
        Day(start + timedelta(i), 1.0, 1.0, 1.0, 1.0, 1e6, 1e8)
        if category == "estavel"
        else Day(
            start + timedelta(i),
            (1 + drift) ** i * 0.99,
            (1 + drift) ** i * 1.01,
            (1 + drift) ** i,
            1.0,
            1e6,
            1e8,
        )
        for i in range(n)
    )
    return PoolSeries(name, chain, "0x" + name, name, 0.003, category, days, 1e8, 1e8)  # type: ignore[arg-type]


def test_janelas_ancoradas_no_fim_sem_sobreposicao() -> None:
    ws = runner.windows(date(2026, 6, 30), date(2025, 1, 1))
    by_len = {length: [w for w in ws if w[0] == length] for length in (90, 180, 365)}
    assert len(by_len[365]) == 1
    assert len(by_len[90]) >= 5
    first, second = by_len[90][0], by_len[90][1]
    assert second[3] == first[2]


def test_pipeline_simulacao_e_relatorio_sem_rede(tmp_path: Path) -> None:
    pools = [
        _pool("p1", "Ethereum", "volatil_maior", 0.0),
        _pool("p2", "Base", "volatil_media", 0.002),
        _pool("p3", "Arbitrum", "estavel", 0.0),
    ]
    rows = runner.run_scenarios(pools, (runner.PRIMARY,))
    assert {r["strategy"] for r in rows} == {"v2", "v3_fixed", "v3_rebal"}
    assert {r["capital"] for r in rows} == set(runner.CAPITALS)
    stable = runner.run_stress(pools)
    assert {r["scenario"] for r in stable} == {"calmo", *runner.STRESS}
    worst = min(r["excess"] for r in stable if r["strategy"] == "v3_fixed" and r["scenario"] == "ust_colapso")
    assert worst < -0.05
    (tmp_path / "results_primary.json").write_text(json.dumps(rows))
    (tmp_path / "results_stress.json").write_text(json.dumps(stable))
    ds = tmp_path / "dataset.json"
    ds.write_text(
        json.dumps(
            {
                "window": {"start": "2025-01-01", "end": "2026-07-14"},
                "generated_at": "x",
                "requests": [{"url": "u", "fetched_at": "t", "sha256": "0" * 64}],
                "skipped": [],
                "pools": [pool_to_json(p) for p in pools],
            }
        )
    )
    path = Path(write_report(tmp_path, ds))
    text = path.read_text(encoding="utf-8")
    assert "Resultado principal" in text
    assert (tmp_path / "lp_report.json").exists()
