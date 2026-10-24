import math
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime, timedelta

import numpy as np

from engine.application.regime import Regime
from engine.application.selector.models import (
    ScoreKey,
    SelectorConfig,
    StrategyScore,
    TradeSample,
    score_key,
)

_DAY = 86_400.0


def max_drawdown_r(r_values: Sequence[float]) -> float:
    peak = equity = worst = 0.0
    for r in r_values:
        equity += r
        peak = max(peak, equity)
        worst = max(worst, peak - equity)
    return worst


def recency_weights(ages_days: Sequence[float], config: SelectorConfig) -> np.ndarray:
    ages = np.maximum(np.asarray(ages_days, dtype=np.float64), 0.0)
    return np.maximum(config.min_weight, 0.5 ** (ages / config.half_life_days))


def compute_score(
    strategy: str,
    regime: Regime,
    symbol: str,
    samples: Sequence[TradeSample],
    as_of: datetime,
    config: SelectorConfig,
) -> StrategyScore:
    """Score de (estrategia, regime, par) SO com trades encerrados ate `as_of` (sem vazamento)."""
    known = sorted((s for s in samples if s.closed_at <= as_of), key=lambda s: s.closed_at)
    count = len(known)
    reason: str | None = None
    if count < config.min_trades:
        reason = f"amostra insuficiente ({count} de {config.min_trades} trades)"
    if count == 0:
        return StrategyScore(
            strategy=strategy, regime=regime, symbol=symbol, as_of=as_of, trades=0, effective_trades=0.0,
            mean_r=0.0, std_error=0.0, lower_bound_r=0.0, max_drawdown_r=0.0, instability=0.0, score=0.0,
            eligible=False, ineligible_reason=reason,
        )  # fmt: skip
    r = np.array([s.r_multiple for s in known], dtype=np.float64)
    ages = [(as_of - s.closed_at).total_seconds() / _DAY for s in known]
    w = recency_weights(ages, config)
    total_w = float(w.sum())
    n_eff = total_w**2 / float((w**2).sum())
    mean = float((w * r).sum() / total_w)
    variance = float((w * (r - mean) ** 2).sum() / total_w)
    variance = variance * n_eff / (n_eff - 1) if n_eff > 1 else 0.0
    std_error = math.sqrt(variance / n_eff) if n_eff > 0 else 0.0
    lower = mean - config.confidence_z * std_error
    drawdown = max_drawdown_r(r.tolist())
    chunks = np.array_split(r, config.instability_windows) if count >= config.instability_windows else []
    instability = float(np.std([c.mean() for c in chunks if len(c)])) if chunks else 0.0
    score = lower - config.drawdown_weight * drawdown / count - config.instability_weight * instability
    if reason is None and n_eff < config.min_effective_trades:
        reason = f"amostra efetiva insuficiente ({n_eff:.1f} de {config.min_effective_trades:g} trades)"
    return StrategyScore(
        strategy=strategy,
        regime=regime,
        symbol=symbol,
        as_of=as_of,
        trades=count,
        effective_trades=n_eff,
        mean_r=mean,
        std_error=std_error,
        lower_bound_r=lower,
        max_drawdown_r=drawdown,
        instability=instability,
        score=score,
        eligible=reason is None,
        ineligible_reason=reason,
    )


def build_scores(
    samples: Sequence[TradeSample],
    as_of: datetime,
    config: SelectorConfig,
    *,
    lookback_days: int | None = None,
) -> dict[ScoreKey, StrategyScore]:
    """Scores por (estrategia, regime, par). Ignora regimes sem operacao e trades encerrados apos `as_of`."""
    groups: dict[ScoreKey, list[TradeSample]] = defaultdict(list)
    floor = as_of - timedelta(days=lookback_days) if lookback_days is not None else None
    for s in samples:
        if not s.operable or s.closed_at > as_of or (floor is not None and s.closed_at < floor):
            continue
        groups[score_key(s.strategy, s.regime, s.symbol)].append(s)
    return {
        key: compute_score(key[0], Regime(key[1]), key[2], items, as_of, config)
        for key, items in groups.items()
    }
