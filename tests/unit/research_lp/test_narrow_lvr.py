from __future__ import annotations

import pytest

from engine.research.lp.liquidity import liquidity_for_value
from engine.research.lp.narrow.lvr import lvr_rate
from engine.research.lp.narrow.sim import Rule, simulate_path
from tests.unit.research_lp.synth import params, path_of, random_walk_bars, v2_profile


def test_lvr_instantaneo_equivale_a_sigma2_sobre_8_do_valor_em_v2() -> None:
    liq, price, sigma2 = 1000.0, 100.0, 1e-5
    assert lvr_rate(sigma2, liq, price) == pytest.approx(sigma2 / 8 * (2 * liq * price**0.5))


def test_lvr_acumulado_fixo_em_faixa_larga_bate_com_formula_fechada() -> None:
    bars = random_walk_bars(1500, 0.004, 31)
    res = simulate_path(path_of(bars), v2_profile(), params(0.5, Rule("fixed")))
    liq = liquidity_for_value(1000.0, bars[0].open, bars[0].open * 0.5, bars[0].open * 1.5)
    sigma2 = path_of(bars).sigma2_hour
    mean_sqrt_p = sum(b.close**0.5 for b in bars) / len(bars)
    expected = 0.25 * sigma2 * len(bars) * liq * mean_sqrt_p
    assert res.lvr_theory_usd == pytest.approx(expected, rel=0.05)


@pytest.mark.parametrize("width", [0.02, 0.05])
def test_perda_simulada_com_giros_tem_a_ordem_de_grandeza_do_lvr_teorico(width: float) -> None:
    ratios = []
    for seed in range(4):
        path = path_of(random_walk_bars(2000, 0.004, 100 + seed))
        res = simulate_path(path, v2_profile(), params(width, Rule("pre", 0.8)))
        assert res.rebalances > 3
        ratios.append(-res.il_usd / res.lvr_theory_usd)
    assert 0.7 < sum(ratios) / len(ratios) < 1.3
