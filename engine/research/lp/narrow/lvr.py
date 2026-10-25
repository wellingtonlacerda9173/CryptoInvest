from __future__ import annotations

from math import sqrt


def lvr_rate(sigma2_per_unit_time: float, liquidity: float, price: float) -> float:
    """LVR instantaneo de posicao v3 dentro da faixa (Milionis et al.): sigma^2/4 x L x sqrt(P) (USD/tempo).

    Para produto constante equivale a sigma^2/8 x valor do pool, pois V = 2 L sqrt(P); em v3 vale para a
    liquidez L ativa, e e zero fora da faixa. Se o preco e martingale, E[posicao - HODL] = -integral.
    """
    return 0.25 * sigma2_per_unit_time * liquidity * sqrt(price)
