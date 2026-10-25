from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence
from itertools import accumulate
from math import sqrt

from engine.research.lp.liquidity import unit_value


class Profile:
    """Liquidez dos OUTROS LPs em funcao do preco, em unidades 'humanas' (USD por unidade de sqrt(preco)).

    `edges` sao razoes x = sqrt(P)/sqrt(P_snapshot) em ordem crescente; `dens` tem len(edges)+1 valores
    (cauda esquerda, intervalos, cauda direita). Pressuposto: o perfil relativo ao preco corrente e
    estacionario e o TVL em USD e constante; assim a medida de volume sobre [sa, sb] e
    sqrt(P_snapshot) x (C(sb/sref) - C(sa/sref)) independente do preco de referencia.
    """

    __slots__ = ("_cum", "dens", "edges", "mult", "sqrt_p_snap")

    def __init__(
        self, edges: Sequence[float], dens: Sequence[float], sqrt_p_snap: float, mult: float = 1.0
    ) -> None:
        if len(dens) != len(edges) + 1:
            raise ValueError("dens deve ter len(edges)+1 valores")
        self.edges = tuple(edges)
        self.dens = tuple(d * mult for d in dens)
        self.sqrt_p_snap = sqrt_p_snap
        self.mult = mult
        widths = [b - a for a, b in zip(self.edges, self.edges[1:], strict=False)]
        self._cum = (0.0, *accumulate(d * w for d, w in zip(self.dens[1:-1], widths, strict=False)))

    @classmethod
    def v2_equivalent(cls, tvl_usd: float, price_snap: float, kappa: float = 1.0) -> Profile:
        """Hipotese: todos os outros em faixa total; kappa multiplica a profundidade."""
        sp = sqrt(price_snap)
        return cls([1.0], [tvl_usd / (2 * sp) * kappa] * 2, sp)

    def scaled(self, mult: float) -> Profile:
        return Profile(self.edges, [d / self.mult for d in self.dens], self.sqrt_p_snap, mult)

    def _cumulative(self, x: float) -> float:
        i = bisect_right(self.edges, x)
        if i == 0:
            return self.dens[0] * (x - self.edges[0])
        return self._cum[i - 1] + self.dens[i] * (x - self.edges[i - 1])

    def measure(self, price_a: float, price_b: float, ref_price: float) -> float:
        """Volume (USD de quote) que cruza [a, b] por unidade de fracao: integral de L_outros d sqrt(P)."""
        sref = sqrt(ref_price)
        lo, hi = sorted((sqrt(price_a) / sref, sqrt(price_b) / sref))
        return self.sqrt_p_snap * (self._cumulative(hi) - self._cumulative(lo))

    def active_depth_usd(self) -> float:
        """L_ativo x sqrt(P): profundidade em USD ao preco corrente (para impacto de preco)."""
        return self.dens[bisect_right(self.edges, 1.0)] * self.sqrt_p_snap


def effective_depth(profile: Profile, half_width: float) -> tuple[float, float]:
    """(L medio dos outros em +-w, capital em USD que daria 50% da liquidez da faixa) ao preco do snapshot."""
    pa, pb = 1.0 - half_width, 1.0 + half_width
    sp = profile.sqrt_p_snap
    mean_l = (profile.measure(pa * sp * sp, pb * sp * sp, sp * sp)) / (sp * (sqrt(pb) - sqrt(pa)))
    return mean_l, mean_l * unit_value(sp * sp, pa * sp * sp, pb * sp * sp)
