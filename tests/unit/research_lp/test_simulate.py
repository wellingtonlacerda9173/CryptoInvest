from __future__ import annotations

import pytest

from engine.research.lp.gas import DEFAULT_GAS, cost_model
from engine.research.lp.simulate import apply_depeg, hodl_usd, simulate
from engine.research.lp.types import CostModel, Strategy
from tests.unit.research_lp.helpers import make_days

FREE = CostModel(0.0, 0.0, 0.0, 0.0)
CAP = 10_000.0


def test_preco_constante_il_zero_e_taxas_puras_v2() -> None:
    days = make_days([1.0] * 31, volume=1e6, tvl=1e8)
    r = simulate(days, Strategy("v2"), CAP, 0.003, FREE)
    share = CAP / (1e8 + CAP)
    assert r.position_usd == pytest.approx(CAP)
    assert r.price_effect_pct == pytest.approx(0.0, abs=1e-12)
    assert r.fees_usd == pytest.approx(30 * 1e6 * 0.003 * share)
    assert r.net_usd - r.hodl_usd == pytest.approx(r.fees_usd)


def test_v2_com_preco_x4_perde_exatamente_20_por_cento_do_hodl() -> None:
    days = make_days([1.0, 4.0], volume=0.0)
    r = simulate(days, Strategy("v2"), CAP, 0.003, FREE)
    assert r.hodl_usd == pytest.approx(CAP * (0.5 + 0.5 * 4))
    assert r.position_usd / r.hodl_usd == pytest.approx(0.8)


def test_grande_movimento_il_domina_as_taxas() -> None:
    days = make_days([1.0, 1.5, 2.0, 3.0, 4.0], volume=1e6, tvl=1e8)
    r = simulate(days, Strategy("v2"), CAP, 0.003, FREE)
    assert r.price_effect_pct < -0.05
    assert r.fees_usd / CAP < 0.01
    assert r.excess_pct < 0


def test_v3_fixa_fora_da_faixa_fica_so_em_quote_e_nao_ganha_taxa() -> None:
    days = make_days([1.0, 1.5, 1.6, 1.7], volume=1e6)
    r = simulate(days, Strategy("v3_fixed", 0.10, 0.5), CAP, 0.003, FREE)
    assert r.time_in_range < 0.5
    assert r.rebalances == 0
    assert 0.0 < r.position_usd < r.hodl_usd


def test_v3_fixa_taxa_so_em_faixa_e_maior_que_v2() -> None:
    days = make_days([1.0] * 11, volume=1e6)
    v2 = simulate(days, Strategy("v2"), CAP, 0.003, FREE)
    v3 = simulate(days, Strategy("v3_fixed", 0.10, 0.0), CAP, 0.003, FREE)
    assert v3.time_in_range == 1.0
    assert v3.fees_usd > v2.fees_usd * 5


def test_rho_um_neutraliza_a_vantagem_de_concentracao() -> None:
    days = make_days([1.0] * 11, volume=1e6)
    v2 = simulate(days, Strategy("v2"), CAP, 0.003, FREE)
    v3 = simulate(days, Strategy("v3_fixed", 0.10, 1.0), CAP, 0.003, FREE)
    assert v3.fees_usd == pytest.approx(v2.fees_usd, rel=0.01)


def test_rebalanceamento_cobra_gas_e_swap_e_reentra_na_faixa() -> None:
    costs = CostModel(gas_open_usd=10.0, gas_close_usd=5.0, gas_swap_usd=3.0, swap_cost_bps=10.0)
    days = make_days([1.0, 1.0, 1.5, 1.5, 1.5], volume=1e6)
    r = simulate(days, Strategy("v3_rebal", 0.10, 0.5), CAP, 0.003, costs)
    assert r.rebalances == 1
    assert r.gas_usd == pytest.approx(10 + 5 + (5 + 10 + 3))
    assert r.swap_usd > 0
    fixed = simulate(days, Strategy("v3_fixed", 0.10, 0.5), CAP, 0.003, costs)
    assert r.time_in_range > fixed.time_in_range


def test_gas_domina_posicao_pequena_em_l1() -> None:
    days = make_days([1.0] * 91, volume=2e6, tvl=1e8)
    cm = cost_model(DEFAULT_GAS["Ethereum"])
    small = simulate(days, Strategy("v2"), 1_000.0, 0.003, cm)
    big = simulate(days, Strategy("v2"), 25_000.0, 0.003, cm)
    assert small.gas_usd / 1_000.0 > 10 * big.gas_usd / 25_000.0
    assert cost_model(DEFAULT_GAS["Base"]).gas_open_usd < cm.gas_open_usd / 10


def test_hodl_avalia_em_usd_com_quote_volatil() -> None:
    days = make_days([1.0, 2.0])
    assert hodl_usd(days, 100.0) == pytest.approx(150.0)


def test_estavel_em_depeg_lp_fixa_perde_para_hodl() -> None:
    base = make_days([1.0] * 60, volume=5e6, tvl=1e8)
    shocked = apply_depeg(base, 20, [0.97, 0.90, 0.80, 0.50, 0.2], 10.0)
    r = simulate(shocked, Strategy("v3_fixed", 0.005, 0.5), CAP, 0.0001, FREE)
    assert r.excess_pct < -0.05
    calm = simulate(base, Strategy("v3_fixed", 0.005, 0.5), CAP, 0.0001, FREE)
    assert calm.excess_pct > 0


def test_apply_depeg_nao_altera_antes_do_inicio_e_mantem_ultimo_multiplicador() -> None:
    base = make_days([1.0] * 10)
    out = apply_depeg(base, 4, [0.9, 0.5], 3.0)
    assert [d.close for d in out[:4]] == [1.0] * 4
    assert out[4].close == pytest.approx(0.9)
    assert out[-1].close == pytest.approx(0.5)
    assert out[5].volume_usd == pytest.approx(3e6)
