from decimal import Decimal, InvalidOperation

from pydantic import Field

from engine.application.risk.rejections import Rejection, RejectionCode
from engine.domain.base import DomainModel
from engine.domain.types import Price, Quantity, StrictDecimal, Symbol, Volume


class SymbolRules(DomainModel):
    """Filtros da corretora (PRICE_FILTER / LOT_SIZE / NOTIONAL) para um simbolo."""

    symbol: Symbol
    tick_size: Price
    step_size: Quantity
    min_quantity: Volume = Decimal(0)
    max_quantity: Quantity | None = None
    min_notional: Volume = Decimal(0)


def floor_to_step(quantity: Decimal, step: Decimal) -> Decimal:
    """Maior multiplo de `step` <= `quantity` (>= 0). ValueError se entrada invalida ou fora da precisao."""
    if not quantity.is_finite() or not step.is_finite() or step <= 0 or quantity < 0:
        raise ValueError(f"arredondamento invalido: valor {quantity}, passo {step}")
    try:
        result = (quantity // step) * step
    except InvalidOperation as error:
        raise ValueError(f"valor {quantity} e passo {step} excedem a precisao decimal") from error
    if not result <= quantity < result + step:
        raise ValueError(f"valor {quantity} e passo {step} excedem a precisao decimal")
    return result


def ceil_to_step(value: Decimal, step: Decimal) -> Decimal:
    floored = floor_to_step(value, step)
    return floored if floored == value else floored + step


def meets_minimums(quantity: Decimal, price: Decimal, rules: SymbolRules) -> Rejection | None:
    if quantity <= 0 or quantity < rules.min_quantity:
        return Rejection(
            code=RejectionCode.QUANTITY_BELOW_MIN,
            message=f"quantidade {quantity} abaixo do minimo {rules.min_quantity}",
        )
    if quantity * price < rules.min_notional:
        return Rejection(
            code=RejectionCode.NOTIONAL_BELOW_MIN,
            message=f"notional {quantity * price} abaixo do minimo {rules.min_notional}",
        )
    return None


def net_reward_risk_ok(
    entry: Decimal, stop_loss: Decimal, take_profit: Decimal, fee_rate: Decimal, min_reward_risk: Decimal
) -> bool:
    """R:R liquido de taxas de ida e volta, com a mesma perda por unidade usada no dimensionamento."""
    loss = loss_per_unit_at_stop(entry, stop_loss, fee_rate)
    reward = (take_profit - entry) - fee_rate * (entry + take_profit)
    return loss > 0 and reward > 0 and reward >= min_reward_risk * loss


def min_target_for_net_reward_risk(
    entry: Decimal, stop_loss: Decimal, fee_rate: Decimal, min_reward_risk: Decimal
) -> Decimal:
    """Menor alvo cujo R:R liquido (`net_reward_risk_ok`) atinge `min_reward_risk`, na mesma base de perda."""
    loss = loss_per_unit_at_stop(entry, stop_loss, fee_rate)
    return (min_reward_risk * loss + entry * (1 + fee_rate)) / (1 - fee_rate)


class PositionSize(DomainModel):
    quantity: Quantity
    risk_amount: StrictDecimal = Field(ge=0)


def loss_per_unit_at_stop(entry: Decimal, stop_loss: Decimal, fee_rate: Decimal) -> Decimal:
    """Perda por unidade se o stop executar: distancia + taxa de entrada + taxa de saida (ida e volta)."""
    return (entry - stop_loss) + fee_rate * (entry + stop_loss)


def size_position(
    *,
    equity: Decimal,
    risk_fraction: Decimal,
    max_position_fraction: Decimal,
    entry: Price,
    stop_loss: Price,
    rules: SymbolRules,
    fee_rate: Decimal,
) -> PositionSize | Rejection:
    """qty = capital * risco / perda por unidade no stop (com taxas), PARA BAIXO ao step, com tetos."""
    inputs = (equity, risk_fraction, max_position_fraction, entry, stop_loss, fee_rate)
    if not all(value.is_finite() for value in inputs):
        return Rejection(code=RejectionCode.INVALID_EQUITY, message="entrada numerica nao finita")
    if equity <= 0 or entry <= stop_loss or stop_loss <= 0 or fee_rate < 0:
        return Rejection(
            code=RejectionCode.INVALID_EQUITY, message="capital ou niveis invalidos para dimensionar"
        )
    loss_unit = loss_per_unit_at_stop(entry, stop_loss, fee_rate)
    capped = min(equity * risk_fraction / loss_unit, equity * max_position_fraction / entry)
    if rules.max_quantity is not None:
        capped = min(capped, rules.max_quantity)
    quantity = floor_to_step(capped, rules.step_size)
    for price in (entry, stop_loss):
        if (rejection := meets_minimums(quantity, price, rules)) is not None:
            return rejection
    return PositionSize(quantity=quantity, risk_amount=quantity * loss_unit)
