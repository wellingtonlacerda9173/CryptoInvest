from collections.abc import Mapping, Sequence

from engine.application.regime import Regime, RegimeReading
from engine.application.selector.models import (
    Choice,
    ScoreKey,
    SelectionDecision,
    SelectorConfig,
    StrategyEvaluation,
    StrategyScore,
    score_key,
)

NO_OPERATION_REGIME = "regime sem operação"


def _evaluate(
    strategy: str, score: StrategyScore | None, config: SelectorConfig, *, operable: bool
) -> StrategyEvaluation:
    if score is None:
        return StrategyEvaluation(
            strategy=strategy, trades=0, score=None, eligible=False, reason="sem histórico neste regime e par"
        )
    if not operable:
        return StrategyEvaluation(
            strategy=strategy, trades=score.trades, score=None, eligible=False, reason=NO_OPERATION_REGIME
        )
    if not score.eligible:
        return StrategyEvaluation(
            strategy=strategy, trades=score.trades, score=None, eligible=False, reason=score.ineligible_reason
        )
    if score.score < config.min_score:
        reason = f"score {score.score:.3f} abaixo do mínimo {config.min_score:.3f}"
        return StrategyEvaluation(
            strategy=strategy, trades=score.trades, score=score.score, eligible=False, reason=reason
        )
    return StrategyEvaluation(strategy=strategy, trades=score.trades, score=score.score, eligible=True)


def select_strategy(
    regime: Regime,
    symbol: str,
    strategies: Sequence[str],
    scores: Mapping[ScoreKey, StrategyScore],
    config: SelectorConfig,
    *,
    operable: bool = True,
    regime_context: dict[str, object] | None = None,
) -> SelectionDecision:
    """Melhor estrategia (ou ate `max_combined`, ponderadas pelo score), so se atingir o score minimo."""
    evaluated = tuple(
        _evaluate(name, scores.get(score_key(name, regime, symbol)), config, operable=operable)
        for name in strategies
    )

    def decision(chosen: tuple[Choice, ...], reason: str) -> SelectionDecision:
        return SelectionDecision(
            symbol=symbol,
            regime=regime,
            operate=bool(chosen),
            chosen=chosen,
            evaluated=evaluated,
            reason=reason,
            min_score=config.min_score,
            min_trades=config.min_trades,
            regime_context=regime_context,
        )

    if not operable:
        return decision((), f"Não operar: regime {regime.label} sem operação.")
    viable = sorted(
        (e for e in evaluated if e.eligible and e.score is not None),
        key=lambda e: (-(e.score or 0.0), e.strategy),
    )
    if not viable:
        why = "; ".join(f"{e.strategy}: {e.reason}" for e in evaluated) or "nenhuma estratégia configurada"
        return decision((), f"Não operar: nenhuma estratégia elegível no regime {regime.label} ({why}).")
    best = viable[0].score or 0.0
    picked = [e for e in viable if (e.score or 0.0) >= best - config.combine_margin][: config.max_combined]
    total = sum(e.score or 0.0 for e in picked)
    chosen = tuple(Choice(strategy=e.strategy, weight=(e.score or 0.0) / total) for e in picked)
    names = ", ".join(f"{c.strategy} ({c.weight:.0%})" for c in chosen)
    return decision(
        chosen,
        f"Operar com {names} no regime {regime.label}: melhor score {best:.3f} "
        f"(mínimo {config.min_score:.3f}) com {viable[0].trades} trades históricos.",
    )


def select_for_reading(
    reading: RegimeReading,
    symbol: str,
    strategies: Sequence[str],
    scores: Mapping[ScoreKey, StrategyScore],
    config: SelectorConfig,
) -> SelectionDecision:
    return select_strategy(
        reading.regime,
        symbol,
        strategies,
        scores,
        config,
        operable=reading.operable,
        regime_context=reading.to_context(),
    )
