"""Estimativa de custo por tokens. Precos em US$ por 1M de tokens (entrada, saida), tabela editavel.

Fonte dos padroes: tabela de modelos da documentacao da Anthropic em 2026-10 (prompts ate 100K tokens). A
estimativa leva margem para cobrir diferencas de tokenizacao; modelo desconhecido usa o preco mais alto.
"""

import json
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path

Price = tuple[Decimal, Decimal]

DEFAULT_PRICES: dict[str, Price] = {
    "claude-haiku-5-5": (Decimal("0.10"), Decimal("0.50")),
    "claude-haiku-4-5": (Decimal(1), Decimal(5)),
    "claude-sonnet-5-5": (Decimal(2), Decimal(10)),
    "claude-sonnet-5": (Decimal(2), Decimal(10)),
    "claude-sonnet-4-6": (Decimal(3), Decimal(15)),
    "claude-opus-5-5": (Decimal(4), Decimal(20)),
    "claude-opus-5": (Decimal(5), Decimal(25)),
    "claude-opus-4-8": (Decimal(5), Decimal(25)),
    "claude-fable-5-1": (Decimal(10), Decimal(50)),
}
UNKNOWN_MODEL_PRICE: Price = (Decimal(10), Decimal(50))
COST_MARGIN = Decimal("1.25")
_MILLION = Decimal(1_000_000)


def estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    prices: Mapping[str, Price] = DEFAULT_PRICES,
    margin: Decimal = COST_MARGIN,
) -> Decimal:
    price_in, price_out = prices.get(model, UNKNOWN_MODEL_PRICE)
    return (price_in * input_tokens + price_out * output_tokens) / _MILLION * margin


def estimate_input_tokens(*texts: str) -> int:
    """Teto conservador antes da chamada: 1 token a cada 3 caracteres."""
    return sum(len(t) for t in texts) // 3 + 16


def load_prices(path: Path) -> dict[str, Price]:
    """`{"modelo": [entrada, saida]}` por cima dos padroes; arquivo ausente ou invalido => so os padroes."""
    prices = dict(DEFAULT_PRICES)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        for model, pair in raw.items():
            price_in, price_out = (Decimal(str(v)) for v in pair)
            if price_in > 0 and price_out > 0:
                prices[str(model)] = (price_in, price_out)
    except (OSError, ValueError, TypeError, ArithmeticError):
        return dict(DEFAULT_PRICES)
    return prices
