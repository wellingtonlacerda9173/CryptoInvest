from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from enum import StrEnum

from pydantic import Field

from engine.application.risk.sizing import min_target_for_net_reward_risk
from engine.application.strategies.base import StrategyContext
from engine.domain.base import DomainModel
from engine.domain.rules import DEFAULT_MIN_REWARD_RISK
from engine.domain.types import StrictDecimal

_QUANTUM = Decimal("0.00000001")


class StopMode(StrEnum):
    ATR = "atr"
    STRUCTURE = "structure"


class LevelsConfig(DomainModel):
    stop_mode: StopMode = StopMode.STRUCTURE
    atr_stop_multiple: float = Field(default=1.5, gt=0)
    stop_buffer_atr: float = Field(default=0.1, ge=0)
    min_risk_atr: float = Field(default=0.5, ge=0)
    max_risk_atr: float = Field(default=4.0, gt=0)
    reward_risk: StrictDecimal = Field(default=DEFAULT_MIN_REWARD_RISK, ge=DEFAULT_MIN_REWARD_RISK)
    taker_fee_rate: StrictDecimal = Field(default=Decimal("0.001"), ge=0, le=Decimal("0.01"))


def _dec(value: float) -> Decimal:
    return Decimal(repr(float(value)))


def effective_fee_rate(config: LevelsConfig, context: StrategyContext) -> Decimal:
    return config.taker_fee_rate if context.taker_fee_rate is None else context.taker_fee_rate


def compute_levels(
    entry: Decimal, atr_value: float, structure_stop: Decimal, config: LevelsConfig, fee_rate: Decimal
) -> tuple[Decimal, Decimal] | None:
    """Retorna (stop, alvo) ou None se o risco sair da faixa [min, max] x ATR (stop ruidoso ou largo).

    O alvo entrega `reward_risk` LIQUIDO de taxas de ida e volta (o que o gate de risco exige); como a
    taxa so aumenta a distancia, o R:R bruto resultante tambem fica >= `reward_risk`.
    """
    if config.stop_mode is StopMode.ATR:
        raw_stop = entry - _dec(config.atr_stop_multiple * atr_value)
    else:
        raw_stop = structure_stop - _dec(config.stop_buffer_atr * atr_value)
    stop = raw_stop.quantize(_QUANTUM, ROUND_FLOOR)
    if stop <= 0 or stop >= entry:
        return None
    risk = entry - stop
    if not _dec(config.min_risk_atr * atr_value) <= risk <= _dec(config.max_risk_atr * atr_value):
        return None
    # arredonda o alvo para cima: nunca derruba o R:R abaixo do configurado
    target = min_target_for_net_reward_risk(entry, stop, fee_rate, config.reward_risk).quantize(
        _QUANTUM, ROUND_CEILING
    )
    return stop, target


def htf_confluence(context: StrategyContext) -> bool:
    return context.htf_trend == "up"
