import hashlib
from datetime import datetime
from decimal import Decimal
from typing import Literal, Self

from pydantic import model_validator

from engine.application.risk.clock import require_aware
from engine.application.risk.sizing import SymbolRules, ceil_to_step, floor_to_step, meets_minimums
from engine.domain.base import DomainModel
from engine.domain.candle import Candle
from engine.domain.risk import RiskConfig
from engine.domain.trade import ExitReason, Trade
from engine.domain.types import Fee, Price, Quantity, StrictDecimal, Symbol, UtcDatetime


class ManagedPosition(DomainModel):
    """Posicao long sob gestao. O stop so e alterado por `manage_position`, e apenas para cima.

    Diferente de `Position` (dominio), admite stop == entrada (breakeven) e acima dela (trailing).
    `entry_fee` e a taxa total paga na entrada (moeda de cotacao) sobre `initial_quantity`;
    `realized_pnl` e liquido de taxas.
    """

    symbol: Symbol
    entry_price: Price
    initial_quantity: Quantity
    quantity: Quantity
    initial_stop: Price
    stop: Price
    take_profit: Price | None
    highest_price: Price
    entry_fee: Fee = Decimal(0)
    breakeven_reached: bool = False
    partial_done: bool = False
    realized_pnl: StrictDecimal = Decimal(0)
    opened_at: UtcDatetime

    @model_validator(mode="after")
    def _check_levels(self) -> Self:
        if not self.initial_stop < self.entry_price:
            raise ValueError("initial_stop must be below entry_price (long only)")
        if self.stop < self.initial_stop:
            raise ValueError("stop can never be below the initial stop")
        if self.take_profit is not None and not self.take_profit > self.entry_price:
            raise ValueError("take_profit must be above entry_price (long only)")
        if self.quantity > self.initial_quantity:
            raise ValueError("quantity must not exceed initial_quantity")
        if self.highest_price < self.entry_price:
            raise ValueError("highest_price must be >= entry_price")
        return self

    @property
    def risk_per_unit(self) -> Decimal:
        return self.entry_price - self.initial_stop

    @property
    def entry_fee_per_unit(self) -> Decimal:
        return self.entry_fee / self.initial_quantity

    def breakeven_price(self, fee_rate: Decimal) -> Decimal:
        """Preco de saida com PnL liquido zero: entrada + taxa de entrada + taxa de saida."""
        return (self.entry_price + self.entry_fee_per_unit) / (1 - fee_rate)

    def unrealized_pnl(self, mark_price: Decimal) -> Decimal:
        return (mark_price - self.entry_price - self.entry_fee_per_unit) * self.quantity


class ExitFill(DomainModel):
    """Saida (parcial ou total). `fee` e a taxa da saida em moeda de cotacao.

    `manage_position` a preenche com a taxa taker estimada; o executor deve substitui-la pela taxa real
    (`model_copy(update={"fee": ...})`) antes de `to_trade`.
    """

    fill_id: str | None = None
    quantity: Quantity
    price: Price
    reason: ExitReason
    at: UtcDatetime
    fee: Fee = Decimal(0)

    def to_trade(self, position: ManagedPosition) -> Trade:
        """Cada saida (parcial ou total) vira um Trade imutavel, com a taxa de entrada proporcional."""
        return Trade(
            trade_id=self.fill_id,
            symbol=position.symbol,
            quantity=self.quantity,
            entry_price=position.entry_price,
            exit_price=self.price,
            entry_fee=position.entry_fee_per_unit * self.quantity,
            exit_fee=self.fee,
            opened_at=position.opened_at,
            closed_at=self.at,
            exit_reason=self.reason,
        )


class EmergencyExit(DomainModel):
    """Fill no stop aprovado ou abaixo: a posicao nasce perdedora e nao pode ficar sem gestao.

    O executor deve vender `quantity` a mercado imediatamente (`action`) e registrar o resultado real
    (`record_trade`), com `entry_fee` como taxa de entrada paga.
    """

    action: Literal["SELL_MARKET_IMMEDIATELY"] = "SELL_MARKET_IMMEDIATELY"
    symbol: Symbol
    quantity: Quantity
    fill_price: Price
    stop_loss: Price
    entry_fee: Fee
    opened_at: UtcDatetime
    reason: str


class ManagementResult(DomainModel):
    """`position` e None quando encerrada.

    `stop_update` e o stop a (re)colocar na corretora (OCO) com a quantidade restante: vem preenchido
    sempre que o stop sobe OU a quantidade muda (parcial).
    """

    position: ManagedPosition | None
    exits: tuple[ExitFill, ...] = ()
    stop_update: Price | None = None


def exit_fill_id(position: ManagedPosition, at: datetime, reason: ExitReason, sequence: int = 0) -> str:
    """Id deterministico da saida (posicao + instante + motivo + ordem): o retry gera o mesmo `trade_id`."""
    key = f"{position.symbol}|{position.opened_at.isoformat()}|{at.isoformat()}|{reason.value}|{sequence}"
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def _exit(
    position: ManagedPosition,
    quantity: Decimal,
    price: Decimal,
    reason: ExitReason,
    at: datetime,
    fee_rate: Decimal,
    sequence: int = 0,
) -> ExitFill:
    return ExitFill(
        fill_id=exit_fill_id(position, at, reason, sequence),
        quantity=quantity,
        price=price,
        reason=reason,
        at=at,
        fee=quantity * price * fee_rate,
    )


def close_position(
    position: ManagedPosition, price: Decimal, at: datetime, reason: ExitReason, *, fee: Decimal
) -> ExitFill:
    return ExitFill(
        fill_id=exit_fill_id(position, at, reason),
        quantity=position.quantity,
        price=price,
        reason=reason,
        at=at,
        fee=fee,
    )


def manage_position(
    position: ManagedPosition, candle: Candle, rules: SymbolRules, config: RiskConfig
) -> ManagementResult:
    """Avanca a gestao com uma vela FECHADA posterior a abertura. Ordem pessimista e sem esperar:

    1. se a vela tocou o stop vigente (inicio da vela), sai pelo stop (no `open` se houve gap) -- mesmo que
       tambem tenha tocado o alvo;
    2. breakeven (entrada + taxas de ida e volta) ao atingir 1R; parcial no 1o alvo; trailing no restante;
       protecao de lucro pelo fechamento, so quando o fechamento esta acima do breakeven liquido.
    Stops novos valem a partir da proxima vela, sao arredondados para CIMA ao tick e nunca chegam ao
    fechamento (<= maxima) da vela, ficando >= 1 tick abaixo dele: stop de venda no/acima do preco dispara
    na hora e a corretora o recusa; se o alvo do stop
    esta acima disso, o stop sobe so ate o permitido (ou nao muda).
    Cada saida leva `fill_id` deterministico (`exit_fill_id`), idempotente em `RiskState.record_trade`.
    """
    if candle.symbol != position.symbol:
        raise ValueError("candle symbol differs from position symbol")
    if not candle.closed:
        raise ValueError("candle must be closed")
    if require_aware(candle.open_time, "candle.open_time") < position.opened_at:
        raise ValueError("candle opened before the position")
    at = candle.close_time
    fee_rate = config.taker_fee_rate
    tick = rules.tick_size

    if candle.low <= position.stop:
        price = min(position.stop, candle.open)
        return ManagementResult(
            position=None,
            exits=(_exit(position, position.quantity, price, ExitReason.STOP_LOSS, at, fee_rate),),
        )

    risk = position.risk_per_unit
    entry = position.entry_price
    highest = max(position.highest_price, candle.high)
    breakeven_price = position.breakeven_price(fee_rate)
    stop = position.stop
    quantity = position.quantity
    take_profit = position.take_profit
    partial_done = position.partial_done
    realized = position.realized_pnl
    exits: list[ExitFill] = []

    cap = floor_to_step(min(candle.close, candle.high), tick) - tick

    def raised(current: Decimal, wanted: Decimal) -> Decimal:
        return max(current, min(wanted, cap))

    breakeven = position.breakeven_reached or highest >= entry + risk
    if breakeven:
        stop = raised(stop, ceil_to_step(breakeven_price, tick))

    trailing = raised(stop, ceil_to_step(highest - config.trailing_r * risk, tick))
    if not partial_done and take_profit is not None and highest >= take_profit:
        part = floor_to_step(quantity * config.partial_exit_fraction, rules.step_size)
        rest = quantity - part
        if (
            meets_minimums(part, take_profit, rules) is None
            and meets_minimums(rest, take_profit, rules) is None
            and meets_minimums(rest, max(stop, trailing), rules) is None
        ):
            fill = _exit(position, part, take_profit, ExitReason.TAKE_PROFIT, at, fee_rate)
            exits.append(fill)
            realized += (take_profit - entry - position.entry_fee_per_unit) * part - fill.fee
            quantity = rest
        partial_done = True
        take_profit = None

    if partial_done:
        stop = trailing

    peak_gain = highest - entry
    if breakeven and candle.close - entry <= peak_gain * (1 - config.profit_giveback):
        reason = ExitReason.PROFIT_PROTECTION if candle.close > breakeven_price else ExitReason.STOP_LOSS
        exits.append(_exit(position, quantity, candle.close, reason, at, fee_rate, len(exits)))
        return ManagementResult(position=None, exits=tuple(exits))

    updated = position.model_copy(
        update={
            "stop": stop,
            "highest_price": highest,
            "breakeven_reached": breakeven,
            "partial_done": partial_done,
            "take_profit": take_profit,
            "quantity": quantity,
            "realized_pnl": realized,
        }
    )
    changed = stop > position.stop or quantity != position.quantity
    return ManagementResult(position=updated, exits=tuple(exits), stop_update=stop if changed else None)
