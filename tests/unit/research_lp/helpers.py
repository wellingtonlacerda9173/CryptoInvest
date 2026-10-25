from __future__ import annotations

from collections.abc import Sequence
from datetime import date, timedelta

from engine.research.lp.types import Day


def make_days(
    closes: Sequence[float], volume: float = 1e6, tvl: float = 1e8, quote_usd: float = 1.0
) -> list[Day]:
    out: list[Day] = []
    prev = closes[0]
    for i, c in enumerate(closes):
        out.append(
            Day(date(2026, 1, 1) + timedelta(days=i), min(prev, c), max(prev, c), c, quote_usd, volume, tvl)
        )
        prev = c
    return out
