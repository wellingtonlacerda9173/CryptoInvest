"""Formatação pt-BR para textos montados no servidor (vírgula decimal, ponto de milhar)."""

from decimal import Decimal


def number(value: float | Decimal, places: int = 2) -> str:
    text = f"{value:,.{places}f}"
    return text.replace(",", "\0").replace(".", ",").replace("\0", ".")


def pct(value: float | Decimal, places: int = 1) -> str:
    return f"{number(value, places)}%"
