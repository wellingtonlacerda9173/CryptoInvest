from __future__ import annotations

from math import sqrt


def il_v2(ratio: float) -> float:
    """Perda impermanente exata de v2 versus HODL para razao de preco r = P_t/P_0."""
    return 2 * sqrt(ratio) / (1 + ratio) - 1


def unit_value(price: float, pa: float, pb: float) -> float:
    """Valor (em quote) de L=1 em [pa, pb] ao preco dado."""
    return position_value(1.0, price, pa, pb)


def amounts(liquidity: float, price: float, pa: float, pb: float) -> tuple[float, float]:
    """(base, quote) de uma posicao concentrada de liquidez L entre pa e pb."""
    sp, sa, sb = sqrt(price), sqrt(pa), sqrt(pb)
    if price <= pa:
        return liquidity * (1 / sa - 1 / sb), 0.0
    if price >= pb:
        return 0.0, liquidity * (sb - sa)
    return liquidity * (1 / sp - 1 / sb), liquidity * (sp - sa)


def liquidity_for_value(value_quote: float, price: float, pa: float, pb: float) -> float:
    """L tal que a posicao, ao preco dado, vale `value_quote` (em quote). Requer pa < price < pb."""
    return value_quote / unit_value(price, pa, pb)


def position_value(liquidity: float, price: float, pa: float, pb: float) -> float:
    base, quote = amounts(liquidity, price, pa, pb)
    return base * price + quote


def concentration_factor(price: float, pa: float, pb: float) -> float:
    """Liquidez efetiva da faixa versus faixa total (v2) para o mesmo capital, no preco dado."""
    return 2 * sqrt(price) / unit_value(price, pa, pb)


def v2_pool_liquidity(tvl_quote: float, price: float) -> float:
    """L equivalente da liquidez do pool tratada como faixa total: TVL = 2 L sqrt(P)."""
    return tvl_quote / (2 * sqrt(price))


def overlap_fraction(low: float, high: float, pa: float, pb: float) -> float:
    """Fracao do intervalo de preco do dia [low, high] dentro da faixa [pa, pb]."""
    if high <= low:
        return 1.0 if pa <= low <= pb else 0.0
    inside = min(high, pb) - max(low, pa)
    return max(0.0, inside) / (high - low)
