from typing import Self

from pydantic import Field, model_validator

from engine.application.records import Record
from engine.application.regime import RegimeConfig
from engine.application.selector.models import SelectionDecision, SelectorConfig


class SelectorSettings(Record):
    """Opt-in do seletor no pipeline (`selector.enabled`): desligado por padrao, nada muda no comportamento.

    `recalc_hours`: de quanto em quanto tempo os scores sao recalculados (backtest e paper) a partir dos
    trades ja encerrados; `lookback_days` limita a janela de trades considerada (None = todo o historico).
    """

    enabled: bool = False
    selector: SelectorConfig = SelectorConfig()
    regime: RegimeConfig = RegimeConfig()
    recalc_hours: int = Field(default=6, ge=1)
    lookback_days: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.enabled and self.selector.max_combined < 1:
            raise ValueError("max_combined must be >= 1")
        return self


SELECTOR_VETO = "selector_veto"
SELECTOR_IDLE = "selector_idle"
SELECTOR_NO_SCORES = "selector_no_scores"


def selection_insight(decision: SelectionDecision) -> dict[str, object]:
    """Parecer lido pelo painel em `decision.signal`: `regime` (texto), `scores`, `ai_opinion` (vazio)."""
    return {
        "regime": decision.regime.label,
        "regime_code": decision.regime.value,
        "scores": [
            {"strategy": e.strategy, "score": e.score} for e in decision.evaluated if e.score is not None
        ],
        "selection": decision.to_context(),
        "ai_opinion": "",
    }
