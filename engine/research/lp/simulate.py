from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from math import sqrt

from engine.research.lp.liquidity import (
    amounts,
    concentration_factor,
    il_v2,
    liquidity_for_value,
    overlap_fraction,
    position_value,
    v2_pool_liquidity,
)
from engine.research.lp.types import CostModel, Day, SimResult, Strategy


def hodl_usd(days: Sequence[Day], capital_usd: float) -> float:
    """Carteira 50/50 mantida parada, avaliada em USD no ultimo dia."""
    first, last = days[0], days[-1]
    value_q = capital_usd / first.quote_usd
    base = value_q / 2 / first.close
    return (base * last.close + value_q / 2) * last.quote_usd


def simulate(
    days: Sequence[Day], strategy: Strategy, capital_usd: float, fee: float, costs: CostModel
) -> SimResult:
    """Posicao aberta no fechamento do 1o dia; taxas acumuladas em USD; custos como saidas de caixa.

    Aproximacoes: volume do dia distribuido uniformemente em [low, high]; liquidez dos demais LPs
    = kappa x liquidez v2-equivalente do TVL, com kappa = concentracao_propria ** rho.
    """
    if strategy.kind == "hodl":
        end = hodl_usd(days, capital_usd)
        return SimResult(capital_usd, end, end, 0.0, 0.0, 0.0, 0, 1.0)

    first = days[0]
    value_q = capital_usd / first.quote_usd
    price = first.close
    full_range = strategy.kind == "v2"
    kappa_rho = strategy.v2_kappa if full_range else strategy.rho
    pa, pb = _band(price, strategy)
    liq = _liquidity(value_q, price, pa, pb, full_range)
    fees = 0.0
    gas = costs.gas_open_usd
    swap = _swap_cost(value_q / 2, price, pa, pb, liq, first.quote_usd, costs, full_range)
    rebalances = 0
    weighted_range = 0.0

    for day in days[1:]:
        frac = 1.0 if full_range else overlap_fraction(day.low, day.high, pa, pb)
        weighted_range += frac
        if frac > 0:
            fees += _day_fees(day, liq, fee, frac, pa, pb, full_range, kappa_rho)
        price = day.close
        if strategy.kind == "v3_rebal" and not pa < price < pb:
            value_q = position_value(liq, price, pa, pb)
            old = amounts(liq, price, pa, pb)
            pa, pb = _band(price, strategy)
            liq = liquidity_for_value(value_q, price, pa, pb)
            need = amounts(liq, price, pa, pb)
            gas += costs.gas_close_usd + costs.gas_open_usd + costs.gas_swap_usd
            swap += abs(old[0] - need[0]) * price * day.quote_usd * costs.swap_cost_bps / 1e4
            rebalances += 1

    last = days[-1]
    gas += costs.gas_close_usd
    position_q = position_value(liq, price, pa, pb) if not full_range else 2 * liq * sqrt(price)
    return SimResult(
        capital_usd=capital_usd,
        hodl_usd=hodl_usd(days, capital_usd),
        position_usd=position_q * last.quote_usd,
        fees_usd=fees,
        gas_usd=gas,
        swap_usd=swap,
        rebalances=rebalances,
        time_in_range=weighted_range / max(len(days) - 1, 1),
    )


def _band(price: float, strategy: Strategy) -> tuple[float, float]:
    if strategy.kind == "v2":
        return 0.0, float("inf")
    return price * (1 - strategy.half_width), price * (1 + strategy.half_width)


def _liquidity(value_q: float, price: float, pa: float, pb: float, full_range: bool) -> float:
    if full_range:
        return value_q / (2 * sqrt(price))
    return liquidity_for_value(value_q, price, pa, pb)


def _swap_cost(
    value_half_q: float,
    price: float,
    pa: float,
    pb: float,
    liq: float,
    quote_usd: float,
    costs: CostModel,
    full_range: bool,
) -> float:
    """Custo de ajustar o 50/50 inicial a proporcao exigida pela faixa (zero em v2)."""
    if full_range:
        return 0.0
    base_needed_q = amounts(liq, price, pa, pb)[0] * price
    return abs(value_half_q - base_needed_q) * quote_usd * costs.swap_cost_bps / 1e4


def _day_fees(
    day: Day, liq: float, fee: float, frac: float, pa: float, pb: float, full_range: bool, rho: float
) -> float:
    """rho: expoente de concentracao (v3) ou kappa direto (faixa total)."""
    pool_liq = v2_pool_liquidity(day.tvl_usd / day.quote_usd, day.close)
    kappa = rho if full_range else concentration_factor(sqrt(pa * pb), pa, pb) ** rho
    share = liq / (liq + kappa * pool_liq)
    return day.volume_usd * fee * frac * share


def apply_depeg(
    days: Sequence[Day], start: int, multipliers: Sequence[float], volume_mult: float
) -> list[Day]:
    """Sobrepoe um choque de de-peg (cenario de estresse, nao previsao) ao preco do token base.

    Os multiplicadores valem a partir de `start`; o ultimo multiplicador permanece ate o fim.
    """
    out: list[Day] = []
    prev = 1.0
    for i, d in enumerate(days):
        m = multipliers[min(i - start, len(multipliers) - 1)] if i >= start else 1.0
        lo_m, hi_m = min(prev, m), max(prev, m)
        shocked = i >= start and (m != 1.0 or prev != 1.0)
        out.append(
            replace(
                d,
                close=d.close * m,
                low=d.low * lo_m,
                high=d.high * hi_m,
                volume_usd=d.volume_usd * (volume_mult if shocked else 1.0),
            )
        )
        prev = m
    return out


__all__ = ["apply_depeg", "hodl_usd", "il_v2", "simulate"]
