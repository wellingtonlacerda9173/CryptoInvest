"""Contas de desempenho que não dependem do banco (testáveis sozinhas)."""

import math
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from statistics import mean, stdev

from engine.application.backtest.models import EquityPoint

ANNUALIZATION_DAYS = 365


def daily_returns(curve: Sequence[EquityPoint], initial: Decimal) -> list[float]:
    """Retorno de cada dia UTC (último patrimônio do dia contra o do dia anterior; o 1º contra o capital)."""
    last_of_day: dict[object, Decimal] = {}
    for point in sorted(curve, key=lambda p: p.at):
        last_of_day[point.at.astimezone(UTC).date()] = point.equity
    previous = initial
    returns: list[float] = []
    for day in sorted(last_of_day, key=str):
        equity = last_of_day[day]
        if previous > 0:
            returns.append(float(equity / previous - 1))
        previous = equity
    return returns


def sharpe(returns: Sequence[float]) -> float | None:
    """Média/desvio amostral dos retornos diários, anualizado por raiz(365); sem risco livre."""
    if len(returns) < 2:
        return None
    deviation = stdev(returns)
    if deviation == 0:
        return None
    return mean(returns) / deviation * math.sqrt(ANNUALIZATION_DAYS)


def covered_seconds(
    intervals: Iterable[tuple[datetime, datetime | None]], start: datetime, end: datetime
) -> float:
    """Segundos de [start, end] cobertos pela união dos intervalos (fim aberto = `end`)."""
    clipped = sorted(
        (max(a, start), min(b or end, end)) for a, b in intervals if (b or end) > start and a < end
    )
    total = 0.0
    cursor = start
    for begin, finish in clipped:
        begin = max(begin, cursor)
        if finish > begin:
            total += (finish - begin).total_seconds()
            cursor = finish
    return total


def idle_fraction(
    intervals: Iterable[tuple[datetime, datetime | None]], start: datetime, end: datetime
) -> float | None:
    span = (end - start).total_seconds()
    if span <= 0:
        return None
    return max(0.0, 1.0 - covered_seconds(intervals, start, end) / span)


def profit_factor_of(gains: Decimal, losses: Decimal) -> float | None:
    return float(gains / losses) if losses > 0 else None
