from __future__ import annotations

import pytest

from engine.research.lp.narrow.bars import HOUR, build_path, regularize
from engine.research.lp.narrow.sim import (
    GasUsd,
    Rule,
    block_excess,
    simulate_path,
)
from tests.unit.research_lp.synth import (
    flat_bars,
    params,
    path_of,
    random_walk_bars,
    v2_profile,
)

GAS = GasUsd(0.30, 0.20, 0.10)
RULES = [Rule("fixed"), Rule("close"), Rule("pre", 0.8), Rule("pre", 0.8, 6.0)]


def test_regularize_preenche_lacunas_e_encadeia_abertura() -> None:
    rows = [[0, 10, 11, 9, 10.5, 5], [2 * HOUR, 10.7, 12, 10, 11, 7]]
    bars = regularize(rows)
    assert [b.ts for b in bars] == [0, HOUR, 2 * HOUR]
    assert bars[1].volume_usd == 0 and bars[1].close == 10.5
    assert bars[2].open == 10.5 and bars[2].low == 10 and bars[2].high == 12


def test_caminho_tem_duracoes_positivas_e_fecha_cada_hora() -> None:
    path = build_path(random_walk_bars(50, 0.004, 3))
    assert all(s.t2 > s.t1 for s in path.segments)
    assert path.segments[-1].t2 == pytest.approx(50 * 60)
    assert sum(s.closes_bar for s in path.segments) == 50


def test_sem_movimento_so_taxas() -> None:
    res = simulate_path(path_of(flat_bars(240)), v2_profile(), params(0.01, Rule("close")))
    assert res.rebalances == 0
    assert res.il_usd == pytest.approx(0, abs=1e-9)
    assert res.directional_usd == pytest.approx(0, abs=1e-9)
    assert res.position_usd == pytest.approx(1000)
    assert res.fees_usd > 0
    assert res.excess_usd == pytest.approx(res.fees_usd - res.gas_usd - res.swap_usd)


def test_tendencia_forte_perda_domina() -> None:
    bars = random_walk_bars(200, 0.001, 1, drift_hour=0.006, volume=2e3)
    res = simulate_path(path_of(bars), v2_profile(), params(0.01, Rule("close"), gas=GAS))
    assert res.rebalances > 20
    assert -res.il_usd > res.fees_usd
    assert res.excess_usd < 0


@pytest.mark.parametrize("rule", RULES)
@pytest.mark.parametrize("delay", [0.0, 15.0])
def test_decomposicao_soma_exatamente_ao_pnl(rule: Rule, delay: float) -> None:
    path = path_of(random_walk_bars(400, 0.004, 7))
    res = simulate_path(path, v2_profile(), params(0.005, rule, gas=GAS, delay_min=delay))
    parts = res.fees_usd - res.gas_usd - res.swap_usd + res.il_usd + res.directional_usd
    assert parts == pytest.approx(res.excess_usd, abs=1e-8)
    assert res.net_usd - res.hodl_usd == pytest.approx(res.excess_usd)


def test_giro_respeita_cooldown() -> None:
    path = path_of(random_walk_bars(600, 0.004, 11))
    free = simulate_path(path, v2_profile(), params(0.0025, Rule("pre", 0.8)))
    held = simulate_path(path, v2_profile(), params(0.0025, Rule("pre", 0.8, 6.0)))
    gaps_free = [b - a for a, b in zip(free.rebalance_min, free.rebalance_min[1:], strict=False)]
    gaps_held = [b - a for a, b in zip(held.rebalance_min, held.rebalance_min[1:], strict=False)]
    assert min(gaps_free) < 360
    assert min(gaps_held) >= 360 - 1e-6
    assert held.rebalances < free.rebalances


def test_gas_e_swap_aplicados_por_giro() -> None:
    path = path_of(random_walk_bars(300, 0.004, 5))
    deep = v2_profile(1e13)
    free = simulate_path(path, deep, params(0.01, Rule("close"), gas=GAS, swap_fee=0.0))
    one = simulate_path(path, deep, params(0.01, Rule("close"), swap_fee=0.0005))
    two = simulate_path(path, deep, params(0.01, Rule("close"), swap_fee=0.0010))
    n = free.rebalances
    assert n > 5
    assert free.gas_usd == pytest.approx(0.30 + n * (0.20 + 0.10 + 0.30) + 0.20)
    assert free.swap_usd == pytest.approx(0, abs=1e-3)
    assert two.swap_usd == pytest.approx(2 * one.swap_usd, rel=1e-3)


def test_atraso_adia_o_giro_exatamente() -> None:
    path = path_of(random_walk_bars(300, 0.004, 9))
    now = simulate_path(path, v2_profile(), params(0.01, Rule("pre", 0.8)))
    late = simulate_path(path, v2_profile(), params(0.01, Rule("pre", 0.8), delay_min=15.0))
    assert late.rebalance_min[0] == pytest.approx(now.rebalance_min[0] + 15.0)


def test_sem_look_ahead() -> None:
    a = random_walk_bars(400, 0.004, 21)
    other = random_walk_bars(400, 0.004, 22)
    mixed = a[:200] + other[200:]
    for rule in RULES:
        full = simulate_path(path_of(a), v2_profile(), params(0.005, rule, gas=GAS), 1e-5)
        alt = simulate_path(path_of(mixed), v2_profile(), params(0.005, rule, gas=GAS), 1e-5)
        assert full.net_series[:200] == pytest.approx(alt.net_series[:200])


def test_bloco_unico_reproduz_excesso_total() -> None:
    path = path_of(random_walk_bars(240, 0.004, 4))
    res = simulate_path(path, v2_profile(), params(0.01, Rule("close"), gas=GAS))
    whole = block_excess(res, 0, res.hours)
    assert whole == pytest.approx(res.excess_pct + GAS.close / res.capital_usd)
    halves = block_excess(res, 0, 120) + block_excess(res, 120, 240)
    assert halves == pytest.approx(whole)


def test_faixa_mais_estreita_ganha_mais_taxa_por_dolar_sem_movimento() -> None:
    path = path_of(flat_bars(100))
    narrow = simulate_path(path, v2_profile(), params(0.0025, Rule("fixed")))
    wide = simulate_path(path, v2_profile(), params(0.05, Rule("fixed")))
    assert narrow.fees_usd > wide.fees_usd
