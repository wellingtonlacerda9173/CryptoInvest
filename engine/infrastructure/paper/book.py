from collections.abc import Sequence
from decimal import Decimal

from engine.domain.market import BookLevel


def sweep(
    levels: Sequence[BookLevel],
    quantity: Decimal,
    *,
    allow_shortfall: bool,
    shortfall_factor: Decimal = Decimal(1),
) -> Decimal | None:
    """Preco medio ponderado ao consumir o book (melhor nivel primeiro).

    Sem liquidez visivel suficiente: `None`, ou, com `allow_shortfall` (saidas que nao podem travar),
    o restante e precificado no pior nivel visivel vezes `shortfall_factor` (slippage extra, liquidez rasa).
    """
    remaining, cost = quantity, Decimal(0)
    for level in levels:
        take = min(remaining, level.quantity)
        cost += take * level.price
        remaining -= take
        if remaining == 0:
            return cost / quantity
    if not allow_shortfall or not levels:
        return None
    return (cost + remaining * levels[-1].price * shortfall_factor) / quantity
