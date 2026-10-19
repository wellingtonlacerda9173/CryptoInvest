from collections.abc import Sequence
from decimal import Decimal
from typing import Literal, Protocol, Self, runtime_checkable

from pydantic import Field, model_validator

from engine.application.strategies.confluence import independent_families
from engine.domain.base import DomainModel
from engine.domain.candle import Candle
from engine.domain.signal import Signal
from engine.domain.types import StrictDecimal

MIN_CONFLUENCES = 3


class StrategyContext(DomainModel):
    """Informacao externa as velas; `htf_trend` (timeframe superior) ausente vale como "nao confirmado".

    `taker_fee_rate`, quando informada (use `RiskConfig.taker_fee_rate`), prevalece sobre a taxa da
    configuracao de niveis da estrategia: o alvo e calculado para o R:R liquido que o gate exige.
    """

    htf_trend: Literal["up", "down", "flat"] | None = None
    taker_fee_rate: StrictDecimal | None = Field(default=None, ge=0, le=Decimal("0.01"))


class StrategySignal(DomainModel):
    """Sinal de dominio acrescido do raciocinio da estrategia (o `Signal` nao carrega confianca).

    `triggers` sao os pre-requisitos do setup (nao contam). `confluences` sao as confirmacoes adicionais;
    so contam FAMILIAS distintas (volume, tendencia, momentum, estrutura), nao indicadores correlacionados.
    """

    signal: Signal
    confidence: float = Field(ge=0, le=1)
    rationale: str = Field(min_length=1)
    triggers: tuple[str, ...] = ()
    confluences: tuple[str, ...]

    @model_validator(mode="after")
    def _check_confluences(self) -> Self:
        if len(independent_families(self.confluences)) < MIN_CONFLUENCES:
            raise ValueError(f"at least {MIN_CONFLUENCES} independent confluence families required")
        return self


@runtime_checkable
class Strategy(Protocol):
    name: str

    def evaluate(self, candles: Sequence[Candle], context: StrategyContext) -> StrategySignal | None:
        """Avalia a ultima vela do historico (apenas velas fechadas). Na duvida, retorna None."""
        ...
