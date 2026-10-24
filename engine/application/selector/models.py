from datetime import datetime
from typing import Self

from pydantic import Field, model_validator

from engine.application.records import Record
from engine.application.regime import Regime


class TradeSample(Record):
    """Trade historico (backtest/paper/live) reduzido ao que o score e a validacao precisam.

    `regime`/`operable` sao os conhecidos na ENTRADA (ultima vela fechada). `stop_pct`/`target_pct` sao as
    distancias relativas ao preco de entrada (base das entradas aleatorias de referencia).
    """

    strategy: str
    symbol: str
    regime: Regime
    operable: bool = True
    opened_at: datetime
    closed_at: datetime
    r_multiple: float
    return_frac: float = 0.0
    stop_pct: float | None = Field(default=None, gt=0)
    target_pct: float | None = Field(default=None, gt=0)
    source: str = "backtest"

    @model_validator(mode="after")
    def _check_times(self) -> Self:
        if self.closed_at < self.opened_at:
            raise ValueError("closed_at must be >= opened_at")
        return self


class SelectorConfig(Record):
    """Parametros do seletor (todos configuraveis).

    O score e: limite inferior (z unilateral) da expectativa em R ponderada por recencia, menos penalidade
    de drawdown (em R, por trade) e de instabilidade (desvio-padrao da expectativa entre `instability_windows`
    fatias cronologicas). Peso de recencia = max(`min_weight`, 0.5^(idade/`half_life_days`)): trades antigos
    nunca pesam menos que `min_weight` do mais recente, e o tamanho efetivo (Kish) entra no erro-padrao.
    """

    min_trades: int = Field(default=30, ge=3)
    min_effective_trades: float = Field(default=15.0, ge=2)
    half_life_days: float = Field(default=90.0, gt=0)
    min_weight: float = Field(default=0.5, gt=0, le=1)
    confidence_z: float = Field(default=1.645, ge=0)
    drawdown_weight: float = Field(default=0.5, ge=0)
    instability_weight: float = Field(default=0.5, ge=0)
    instability_windows: int = Field(default=3, ge=2)
    min_score: float = 0.05
    max_combined: int = Field(default=1, ge=1)
    combine_margin: float = Field(default=0.05, ge=0)

    @model_validator(mode="after")
    def _check_weights(self) -> Self:
        # pesos = score / soma dos scores: com score <= 0 a soma pode zerar ou inverter o sinal dos pesos
        if self.max_combined > 1 and self.min_score <= 0:
            raise ValueError("min_score deve ser > 0 quando max_combined > 1 (pesos exigem scores positivos)")
        return self


class StrategyScore(Record):
    strategy: str
    regime: Regime
    symbol: str
    as_of: datetime
    trades: int
    effective_trades: float
    mean_r: float
    std_error: float
    lower_bound_r: float
    max_drawdown_r: float
    instability: float
    score: float
    eligible: bool
    ineligible_reason: str | None = None


class StrategyEvaluation(Record):
    strategy: str
    trades: int
    score: float | None
    eligible: bool
    reason: str | None = None


class Choice(Record):
    strategy: str
    weight: float = Field(gt=0, le=1)


class SelectionDecision(Record):
    """Decisao auditavel do seletor, pronta para `decision.signal/context` (`to_context`)."""

    symbol: str
    regime: Regime
    operate: bool
    chosen: tuple[Choice, ...]
    evaluated: tuple[StrategyEvaluation, ...]
    reason: str
    min_score: float
    min_trades: int
    regime_context: dict[str, object] | None = None

    @property
    def chosen_label(self) -> str:
        return " + ".join(c.strategy for c in self.chosen) if self.chosen else "nenhuma"

    def weight_of(self, strategy: str) -> float:
        return next((c.weight for c in self.chosen if c.strategy == strategy), 0.0)

    def to_context(self) -> dict[str, object]:
        return {**self.model_dump(mode="json"), "chosen_label": self.chosen_label}


ScoreKey = tuple[str, str, str]


def score_key(strategy: str, regime: Regime, symbol: str) -> ScoreKey:
    return strategy, regime.value, symbol
