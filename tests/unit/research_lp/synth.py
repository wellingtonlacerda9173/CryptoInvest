from __future__ import annotations

import random
from math import exp

from engine.research.lp.narrow.bars import HOUR, Bar, PathData, build_path, regularize
from engine.research.lp.narrow.profile import Profile
from engine.research.lp.narrow.sim import GasUsd, Params, Rule

ZERO_GAS = GasUsd(0.0, 0.0, 0.0)


def random_walk_bars(
    hours: int,
    sigma_hour: float,
    seed: int,
    start: float = 100.0,
    drift_hour: float = 0.0,
    volume: float = 1e6,
) -> list[Bar]:
    rng = random.Random(seed)  # noqa: S311
    step_sigma = sigma_hour / 60**0.5
    rows: list[list[float]] = []
    price = start
    for h in range(hours):
        o = price
        hi = lo = price
        for _ in range(60):
            price *= exp(rng.gauss(drift_hour / 60, step_sigma))
            hi, lo = max(hi, price), min(lo, price)
        rows.append([h * HOUR, o, hi, lo, price, volume])
    return regularize(rows)


def flat_bars(hours: int, price: float = 100.0, volume: float = 1e6) -> list[Bar]:
    return regularize([[h * HOUR, price, price, price, price, volume] for h in range(hours)])


def v2_profile(tvl: float = 1e8, price: float = 100.0, kappa: float = 1.0) -> Profile:
    return Profile.v2_equivalent(tvl, price, kappa)


def path_of(bars: list[Bar]) -> PathData:
    return build_path(bars)


def params(
    width: float,
    rule: Rule,
    capital: float = 1000.0,
    gas: GasUsd = ZERO_GAS,
    swap_fee: float = 0.0005,
    delay_min: float = 0.0,
    competition: float = 0.0,
) -> Params:
    return Params(capital, width, rule, 0.0005, gas, swap_fee, delay_min, competition)
