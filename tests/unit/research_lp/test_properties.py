from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from engine.research.lp.liquidity import amounts, il_v2, liquidity_for_value, position_value
from engine.research.lp.simulate import simulate
from engine.research.lp.types import CostModel, Strategy
from tests.unit.research_lp.helpers import make_days

FREE = CostModel(0.0, 0.0, 0.0, 0.0)
price = st.floats(0.01, 100.0)
width = st.floats(0.001, 0.9)
path = st.lists(st.floats(0.05, 20.0), min_size=2, max_size=40)


@given(liq=st.floats(0.1, 1e6), p=price, w=width, mult=st.floats(0.01, 100.0))
def test_valor_da_posicao_nunca_negativo(liq: float, p: float, w: float, mult: float) -> None:
    pa, pb = p * (1 - w), p * (1 + w)
    base, quote = amounts(liq, p * mult, pa, pb)
    assert base >= 0
    assert quote >= 0
    assert position_value(liq, p * mult, pa, pb) >= 0


@given(value=st.floats(1.0, 1e7), p=price, w=width)
def test_conservacao_no_mint(value: float, p: float, w: float) -> None:
    pa, pb = p * (1 - w), p * (1 + w)
    liq = liquidity_for_value(value, p, pa, pb)
    base, quote = amounts(liq, p, pa, pb)
    assert base * p + quote == pytest.approx(value, rel=1e-9)


@given(closes=path)
@settings(max_examples=60, deadline=None)
def test_lp_v2_sem_taxas_nunca_supera_hodl(closes: list[float]) -> None:
    r = simulate(make_days([1.0, *closes], volume=0.0), Strategy("v2"), 1000.0, 0.003, FREE)
    assert r.net_usd <= r.hodl_usd * (1 + 1e-9)
    assert r.position_usd >= 0


@given(closes=path, w=width)
@settings(max_examples=60, deadline=None)
def test_v3_sem_volume_nao_tem_taxa_e_valor_nao_negativo(closes: list[float], w: float) -> None:
    for kind in ("v3_fixed", "v3_rebal"):
        r = simulate(make_days([1.0, *closes], volume=0.0), Strategy(kind, w, 0.5), 1000.0, 0.003, FREE)
        assert r.position_usd >= 0
        assert r.fees_usd == 0.0
        assert 0.0 <= r.time_in_range <= 1.0


@given(ratio=st.floats(0.001, 1000.0))
def test_il_v2_sempre_nao_positiva(ratio: float) -> None:
    assert il_v2(ratio) <= 1e-12
