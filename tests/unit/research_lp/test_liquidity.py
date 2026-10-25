from __future__ import annotations

from math import sqrt

import pytest

from engine.research.lp.liquidity import (
    amounts,
    concentration_factor,
    il_v2,
    liquidity_for_value,
    overlap_fraction,
    position_value,
    v2_pool_liquidity,
)


@pytest.mark.parametrize(
    ("ratio", "expected"),
    [(1.0, 0.0), (4.0, -0.2), (0.25, -0.2), (2.0, 2 * sqrt(2) / 3 - 1), (9.0, -0.4)],
)
def test_il_v2_exata(ratio: float, expected: float) -> None:
    assert il_v2(ratio) == pytest.approx(expected)


def test_posicao_dentro_da_faixa_valor_calculado_a_mao() -> None:
    base, quote = amounts(1.0, 2.0, 1.0, 4.0)
    assert base == pytest.approx(1 / sqrt(2) - 0.5)
    assert quote == pytest.approx(sqrt(2) - 1)
    assert position_value(1.0, 2.0, 1.0, 4.0) == pytest.approx((1 / sqrt(2) - 0.5) * 2 + sqrt(2) - 1)


def test_fora_da_faixa_acima_so_quote() -> None:
    base, quote = amounts(1.0, 5.0, 1.0, 4.0)
    assert base == 0.0
    assert quote == pytest.approx(1.0)
    assert position_value(1.0, 5.0, 1.0, 4.0) == pytest.approx(1.0)


def test_fora_da_faixa_abaixo_so_base() -> None:
    base, quote = amounts(1.0, 0.5, 1.0, 4.0)
    assert base == pytest.approx(0.5)
    assert quote == 0.0
    assert position_value(1.0, 0.5, 1.0, 4.0) == pytest.approx(0.25)


def test_valor_continuo_nas_bordas() -> None:
    assert position_value(3.0, 1.0, 1.0, 4.0) == pytest.approx(position_value(3.0, 1.0 + 1e-12, 1.0, 4.0))
    assert position_value(3.0, 4.0, 1.0, 4.0) == pytest.approx(position_value(3.0, 4.0 - 1e-12, 1.0, 4.0))


def test_liquidez_para_valor_e_inversa() -> None:
    liq = liquidity_for_value(1000.0, 2.0, 1.5, 2.6)
    assert position_value(liq, 2.0, 1.5, 2.6) == pytest.approx(1000.0)


def test_faixa_total_vale_dois_l_raiz_p() -> None:
    assert position_value(5.0, 4.0, 0.0, float("inf")) == pytest.approx(2 * 5.0 * 2.0)
    assert concentration_factor(4.0, 1e-12, 1e12) == pytest.approx(1.0, rel=1e-4)


def test_fator_de_concentracao_cresce_com_faixa_estreita() -> None:
    wide = concentration_factor(100.0, 50.0, 200.0)
    narrow = concentration_factor(100.0, 99.0, 101.0)
    assert 1 < wide < narrow
    expected = 2 / (2 - (sqrt(100 / 101) + sqrt(99 / 100)))
    assert narrow == pytest.approx(expected, rel=1e-6)


def test_liquidez_v2_do_pool() -> None:
    assert v2_pool_liquidity(800.0, 4.0) == pytest.approx(200.0)


def test_overlap_fraction() -> None:
    assert overlap_fraction(1.0, 3.0, 2.0, 5.0) == pytest.approx(0.5)
    assert overlap_fraction(1.0, 3.0, 0.0, 5.0) == 1.0
    assert overlap_fraction(1.0, 3.0, 4.0, 5.0) == 0.0
    assert overlap_fraction(2.0, 2.0, 1.0, 3.0) == 1.0
