"""Execucao compartilhada entre o motor de backtest e o runner do paper (mesmo codigo, mesmo resultado)."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from engine.application.risk.position import EmergencyExit, ManagedPosition
from engine.application.risk.sizing import SymbolRules, floor_to_step, meets_minimums
from engine.domain.candle import Candle
from engine.domain.risk import RiskConfig
from engine.domain.trade import ExitReason, Trade
from engine.domain.types import Timeframe

_DAY_SECONDS = 86_400


@dataclass(frozen=True, slots=True)
class PartialPlan:
    quantity: Decimal
    price: Decimal


def plan_partial(position: ManagedPosition, config: RiskConfig, rules: SymbolRules) -> PartialPlan | None:
    """Limit de venda da parcial no 1o alvo; None quando nao da para dividir a posicao respeitando os minimos.

    Sem parcial viavel o estado e o mesmo a que `manage_position` chegaria: trailing na posicao inteira.
    """
    target = position.take_profit
    if target is None:
        return None
    part = floor_to_step(position.quantity * config.partial_exit_fraction, rules.step_size)
    rest = position.quantity - part
    viable = all(
        meets_minimums(quantity, price, rules) is None
        for quantity, price in ((part, target), (rest, target), (rest, position.stop))
    )
    return PartialPlan(part, target) if viable else None


def rolling_quote_volume(window: Sequence[Candle], timeframe: Timeframe) -> Decimal:
    """Volume aproximado das ultimas 24h em moeda de cotacao, a partir das velas da janela."""
    steps = max(1, _DAY_SECONDS // timeframe.seconds)
    return sum((c.close * c.volume for c in window[-steps:]), Decimal(0))


def emergency_trade(
    emergency: EmergencyExit,
    *,
    trade_id: str,
    price: Decimal,
    fee: Decimal,
    at: datetime,
    reason: ExitReason,
) -> Trade:
    return Trade(
        trade_id=trade_id,
        symbol=emergency.symbol,
        quantity=emergency.quantity,
        entry_price=emergency.fill_price,
        exit_price=price,
        entry_fee=emergency.entry_fee,
        exit_fee=fee,
        opened_at=emergency.opened_at,
        closed_at=max(at, emergency.opened_at),
        exit_reason=reason,
    )
