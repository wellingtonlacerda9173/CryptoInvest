from decimal import Decimal

DEFAULT_MIN_REWARD_RISK = Decimal(2)


def reward_risk(entry: Decimal, stop_loss: Decimal, take_profit: Decimal) -> Decimal:
    """Razao retorno/risco de uma operacao long: (alvo - entrada) / (entrada - stop)."""
    risk = entry - stop_loss
    if risk <= 0:
        raise ValueError("stop_loss must be below entry")
    return (take_profit - entry) / risk


def meets_reward_risk(entry: Decimal, stop_loss: Decimal, take_profit: Decimal, minimum: Decimal) -> bool:
    """Compara sem divisao (sem arredondamento): retorno >= minimo * risco."""
    risk = entry - stop_loss
    return risk > 0 and (take_profit - entry) >= minimum * risk
