from __future__ import annotations

import random
from collections.abc import Sequence
from math import comb


def quantile(values: Sequence[float], q: float) -> float:
    s = sorted(values)
    if not s:
        return float("nan")
    pos = q * (len(s) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def median(values: Sequence[float]) -> float:
    return quantile(values, 0.5)


def share_positive(values: Sequence[float]) -> float:
    return sum(v > 0 for v in values) / len(values) if values else float("nan")


def binom_two_sided(k: int, n: int, p: float = 0.5) -> float:
    """Teste binomial exato bicaudal (soma das probabilidades <= a observada)."""
    if n == 0:
        return 1.0
    probs = [comb(n, i) * p**i * (1 - p) ** (n - i) for i in range(n + 1)]
    return min(1.0, sum(x for x in probs if x <= probs[k] * (1 + 1e-9)))


def bootstrap_median_ci(
    values: Sequence[float], draws: int = 2000, alpha: float = 0.05, seed: int = 20260101
) -> tuple[float, float]:
    if not values:
        return float("nan"), float("nan")
    rng = random.Random(seed)  # noqa: S311
    n = len(values)
    meds = sorted(median([values[rng.randrange(n)] for _ in range(n)]) for _ in range(draws))
    return meds[int(alpha / 2 * draws)], meds[int((1 - alpha / 2) * draws) - 1]


def bonferroni_alpha(alpha: float, tests: int) -> float:
    return alpha / max(tests, 1)
