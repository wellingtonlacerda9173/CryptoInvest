from __future__ import annotations

import json
from pathlib import Path

import pytest

from engine.research.lp.narrow.aggregate import calibration_summary, cash_return, decomposition
from engine.research.lp.narrow.bars import build_path
from engine.research.lp.narrow.report import build_report, write_narrow_report
from engine.research.lp.narrow.runner import (
    BASE,
    RULES,
    SENS_RULES,
    SENS_WIDTHS,
    SENSITIVITIES,
    WIDTHS,
    run_primary,
    run_sensitivity,
)
from engine.research.lp.narrow.snapshot import PoolSnapshot
from engine.research.lp.narrow.snapshots import PoolData, depth_table
from tests.unit.research_lp.synth import random_walk_bars

SQRT_P = int(100**0.5 * 2**96)


def make_pool(seed: int) -> PoolData:
    bars = random_walk_bars(1440, 0.004, seed, start=100.0, volume=2e5)
    meta = {
        "pool_id": f"arbitrum:0x{seed:04d}",
        "name": f"USDC / TEST{seed} 0.05%",
        "chain": "Arbitrum",
        "category": "btc" if seed % 2 else "eth",
        "fee": 0.0005,
        "reserve_usd": 5e6,
    }
    snap = PoolSnapshot(1, SQRT_P, 46_054, 10**14, 10, "0xa", "0xb", 18, 18, (), 0, 0)
    return PoolData(meta, build_path(bars), snap, stable_is_token0=False)


@pytest.fixture(scope="module")
def primary() -> dict:
    return run_primary([make_pool(1), make_pool(2), make_pool(3)])


def test_grade_fixada_e_decomposicao_fecha_em_cada_celula(primary: dict) -> None:
    assert primary["meta"]["n_tests"] == len(WIDTHS) * len(RULES) == 42
    for pool in primary["pools"]:
        assert len(pool["results"]) == 42
        for r in pool["results"].values():
            parts = r["fees"] + r["il"] + r["directional"] + r["gas"] + r["swap"]
            assert parts == pytest.approx(r["excess"], abs=1e-9)
            assert len(r["blocks"]) == 2


def test_resumo_traz_distribuicao_e_bonferroni(primary: dict) -> None:
    s = primary["summary"]["1%|preemptiva 80%"]
    assert s["p10"] <= s["median"] <= s["p90"]
    assert 0 <= s["share_pos"] <= 1 and s["n"] == 3
    assert primary["meta"]["bonferroni_alpha"] == pytest.approx(0.05 / 42)


def test_agregacao_e_caixa(primary: dict) -> None:
    d = decomposition(primary["pools"], "1%|preemptiva 80%")
    soma = d["fees"] + d["il"] + d["directional"] + d["gas"] + d["swap"]
    assert soma == pytest.approx(d["excess"], abs=1e-9)
    assert cash_return(8760, 0.045) == pytest.approx(0.045)


def test_sensibilidade_roda_todos_os_cenarios_rotulados() -> None:
    doc = run_sensitivity([make_pool(1)])
    assert set(doc["scenarios"]) == {"base", *(s.key for s in SENSITIVITIES)}
    cells = doc["scenarios"]["base"]["cells"]
    assert len(cells) == len(SENS_WIDTHS) * len(SENS_RULES)
    assert doc["scenarios"]["atraso_60m"]["label"]
    # competicao so reduz taxas: excesso nunca melhora
    comp, base = doc["scenarios"]["competicao_50"]["cells"], cells
    assert all(comp[k]["median"] <= base[k]["median"] + 1e-9 for k in base)


def test_profundidade_por_largura_para_o_relatorio() -> None:
    d = depth_table(make_pool(1))
    assert [w["half_width"] for w in d["widths"]] == [0.005, 0.01, 0.02, 0.05, 0.10]
    assert all(w["usd_for_half_share"] > 0 for w in d["widths"])


def test_calibracao_resumo() -> None:
    win = {
        "skipped": False,
        "volume_events_usd": 100.0,
        "volume_gecko_usd": 100.0,
        "fees_real": {"0.01": 2.0},
        "fees_model": {"0.01": 1.0},
    }
    c = calibration_summary({"pools": [{"windows": [win, {"skipped": True}]}], "failures": {}})
    assert c["n_windows"] == 1 and c["volume_ratio"] == 1.0 and c["geo"]["0.01"] == pytest.approx(2.0)


def test_relatorio_tem_resposta_direta_protocolo_e_avisos(primary: dict, tmp_path: Path) -> None:
    sens = run_sensitivity([make_pool(1)])
    text = build_report(primary, sens, None)
    for needle in (
        "## 1. Resposta direta",
        "Protocolo OPCIONAL",
        "frase-semente",
        "carteira nova e isolada",
        "NÃO foi possível medir",
    ):
        assert needle in text
    assert BASE.label.split(" (")[0] in text or "Sensibilidades" in text
    p, s = tmp_path / "p.json", tmp_path / "s.json"
    p.write_text(json.dumps(primary), encoding="utf-8")
    s.write_text(json.dumps(sens), encoding="utf-8")
    out = write_narrow_report(p, s, tmp_path / "nao_existe.json", tmp_path / "r.md")
    assert Path(out).read_text(encoding="utf-8").startswith("# Faixas estreitas")
