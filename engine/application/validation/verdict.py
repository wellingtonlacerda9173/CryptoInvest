import math

from engine.application.validation.models import (
    BuyHold,
    Judgement,
    RandomReference,
    TradeStats,
    ValidationConfig,
    Verdict,
)

_EPS = 1e-9


def corrected_alpha(alpha: float, comparisons: int, method: str) -> float:
    """Nivel por comparacao que mantem o erro tipo I da familia em `alpha` (Sidak; Bonferroni e mais duro)."""
    m = max(1, comparisons)
    return alpha / m if method == "bonferroni" else 1 - (1 - alpha) ** (1 / m)


def min_attainable_p(simulations: int) -> float:
    """Com `simulations` sorteios e correcao +1, nenhum p-valor e menor que 1/(simulations + 1)."""
    return 1 / (simulations + 1)


def required_simulations(alpha_corrected: float, *, margin: float = 0.2, floor: int = 10_000) -> int:
    """n_min = ceil(1/alfa corrigido) com folga `margin`; nunca menos que `floor`."""
    return max(floor, math.ceil((1 + margin) / alpha_corrected))


def beats_buy_hold(stats: TradeStats, bh: BuyHold) -> bool:
    """Retorno >= comprar-e-segurar, ou melhor retorno por drawdown (so com ambos positivos)."""
    if stats.total_return >= bh.total_return:
        return True
    if stats.total_return > 0 and bh.total_return > 0:
        return stats.total_return / max(stats.max_drawdown, _EPS) >= bh.total_return / max(
            bh.max_drawdown, _EPS
        )
    return False


def judge(
    stats: TradeStats,
    reference: RandomReference | None,
    bh: BuyHold,
    config: ValidationConfig,
    *,
    comparisons: int,
    min_trades: int,
    idle_fraction: float | None = None,
) -> Judgement:
    """Nunca devolve SUPERA com amostra abaixo do minimo, censurada (parada demais), sem referencia, sem poder
    (menor p atingivel >= alfa corrigido) ou sem passar o alfa corrigido.

    Criterio do veredito contra comprar-e-segurar: retorno bruto >= B&H OU retorno/drawdown >= o do B&H
    (ambos positivos). Retorno por unidade de exposicao e apenas informativo (nao decide).
    """
    alpha_c = corrected_alpha(config.alpha, comparisons, config.correction)
    min_p = min_attainable_p(reference.simulations if reference else config.simulations)
    underpowered = min_p >= alpha_c
    p_better = reference.p_better if reference else None
    p_worse = reference.p_worse if reference else None
    notes: list[str] = []
    bh_ok = beats_buy_hold(stats, bh)

    def result(verdict: Verdict) -> Judgement:
        return Judgement(
            verdict=verdict,
            p_better=p_better,
            p_worse=p_worse,
            alpha=config.alpha,
            alpha_corrected=alpha_c,
            comparisons=comparisons,
            correction=config.correction,
            beats_buy_hold=bh_ok,
            min_attainable_p=min_p,
            underpowered=underpowered,
            notes=tuple(notes),
        )

    if stats.trades < min_trades:
        notes.append(f"{stats.trades} trades fora da amostra, mínimo {min_trades}")
        return result(Verdict.AMOSTRA_INSUFICIENTE)
    if idle_fraction is not None and idle_fraction > config.max_idle_fraction:
        notes.append(
            f"estratégia parada em {idle_fraction:.0%} do período fora da amostra "
            f"(limite {config.max_idle_fraction:.0%}): resultado censurado pelas travas de risco"
        )
        return result(Verdict.CENSURADO)
    if reference is None:
        notes.append("sem níveis de stop/alvo para simular entradas aleatórias")
        return result(Verdict.NAO_DISTINGUIVEL)
    if underpowered:
        notes.append(
            f"teste sem poder: menor p atingível {min_p:.6f} >= alfa corrigido {alpha_c:.6f}; "
            "aumente `simulations`"
        )
    if not underpowered and p_better is not None and p_better < alpha_c and (stats.mean_r or 0.0) > 0:
        if bh_ok:
            return result(Verdict.SUPERA)
        notes.append("supera entradas aleatórias, mas não o comprar-e-segurar")
        return result(Verdict.NAO_DISTINGUIVEL)
    if p_worse is not None and p_worse < alpha_c:
        return result(Verdict.PIOR)
    return result(Verdict.NAO_DISTINGUIVEL)
