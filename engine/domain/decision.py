from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from engine.domain.base import DomainModel
from engine.domain.signal import Signal
from engine.domain.types import Symbol, UtcDatetime


class DecisionAction(StrEnum):
    ENTER = "enter"
    EXIT = "exit"
    HOLD = "hold"
    SKIP = "skip"


class Decision(DomainModel):
    """Decisao auditavel: toda acao (ou nao-acao) carrega a justificativa."""

    decided_at: UtcDatetime
    symbol: Symbol
    action: DecisionAction
    reason: str = Field(min_length=1)
    source: str = Field(min_length=1)
    signal: Signal | None = None

    @model_validator(mode="after")
    def _check_signal(self) -> Self:
        if self.action is DecisionAction.ENTER and self.signal is None:
            raise ValueError("enter decision requires a signal")
        return self
