from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
from typing import Any, Final, Literal, Self

from pydantic import Field

from engine.domain.base import DomainModel
from engine.domain.decision import DecisionAction
from engine.domain.order import EntryOrder, ExitOrder, OrderStatus, OrderType
from engine.domain.signal import Signal
from engine.domain.types import Fee, Price, Quantity, Symbol, UtcDatetime, Volume

# o gateway guarda o OCO de cada entrada sob '<entrada>:oco'; o id da entrada tem ate 36 caracteres
OCO_SUFFIX: Final = ":oco"
CLIENT_ORDER_ID_MAX = 64


class RunMode(StrEnum):
    PAPER = "paper"
    LIVE = "live"
    BACKTEST = "backtest"


class ControlAction(StrEnum):
    START = "start"
    STOP = "stop"
    PAUSE = "pause"
    RESUME = "resume"
    REACTIVATE = "reactivate"
    HALT = "halt"


# apos estas acoes o robo esta ativo; apos as demais, nao
ACTIVATING_ACTIONS = frozenset({ControlAction.START, ControlAction.RESUME, ControlAction.REACTIVATE})


class ControlEvent(DomainModel):
    """Ligar/desligar/pausar/reativar. Acoes manuais exigem operador e motivo (automaticas usam 'system')."""

    at: UtcDatetime
    action: ControlAction
    operator: str = Field(min_length=1, max_length=100, pattern=r"\S")
    reason: str = Field(min_length=1, pattern=r"\S")


class RunRecord(DomainModel):
    run_id: str = Field(min_length=1, max_length=64)
    mode: RunMode
    started_at: UtcDatetime
    ended_at: UtcDatetime | None = None
    config_hash: str = Field(min_length=1, max_length=64)
    code_version: str = Field(min_length=1, max_length=64)


class OrderRecord(DomainModel):
    client_order_id: str = Field(min_length=1, max_length=CLIENT_ORDER_ID_MAX)
    symbol: Symbol
    side: Literal["buy", "sell"]
    type: OrderType | Literal["oco"]
    quantity: Quantity
    price: Price | None = None
    stop_loss: Price | None = None
    take_profit: Price | None = None
    status: OrderStatus = OrderStatus.NEW
    exchange_order_id: str | None = None
    filled_quantity: Volume = Decimal(0)
    average_price: Price | None = None
    created_at: UtcDatetime
    updated_at: UtcDatetime

    @classmethod
    def from_order(
        cls,
        order: EntryOrder | ExitOrder,
        *,
        status: OrderStatus = OrderStatus.NEW,
        at: datetime | None = None,
    ) -> Self:
        levels = (
            {"stop_loss": order.stop_loss, "take_profit": order.take_profit}
            if isinstance(order, EntryOrder)
            else {}
        )
        return cls(
            client_order_id=order.client_order_id,
            symbol=order.symbol,
            side=order.side,
            type=order.type,
            quantity=order.quantity,
            price=order.price,
            status=status,
            created_at=order.created_at,
            updated_at=at or order.created_at,
            **levels,
        )

    @classmethod
    def oco_of(
        cls,
        entry: Self,
        *,
        status: OrderStatus = OrderStatus.NEW,
        filled_quantity: Decimal = Decimal(0),
        average_price: Decimal | None = None,
        at: datetime | None = None,
    ) -> Self:
        """Ordem de protecao (stop + alvo) da entrada, para que seus fills tenham ordem a que se ligar."""
        return cls(
            client_order_id=f"{entry.client_order_id}{OCO_SUFFIX}",
            symbol=entry.symbol,
            side="sell",
            type="oco",
            quantity=entry.quantity,
            price=entry.take_profit,
            stop_loss=entry.stop_loss,
            take_profit=entry.take_profit,
            status=status,
            filled_quantity=filled_quantity,
            average_price=average_price,
            created_at=entry.created_at,
            updated_at=at or entry.updated_at,
        )

    @property
    def is_open(self) -> bool:
        return self.status in (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED)


class FillRecord(DomainModel):
    """Execucao. `fill_id` e a chave de idempotencia (id da corretora ou derivado deterministico)."""

    fill_id: str = Field(min_length=1, max_length=64)
    client_order_id: str = Field(min_length=1, max_length=CLIENT_ORDER_ID_MAX)
    symbol: Symbol
    side: Literal["buy", "sell"]
    quantity: Quantity
    price: Price
    fee: Fee = Decimal(0)
    fee_asset: str | None = Field(default=None, max_length=12)
    filled_at: UtcDatetime


class DecisionEntry(DomainModel):
    """Linha do feed de decisoes: operou ou nao operou, e por que. Imutavel no banco."""

    decided_at: UtcDatetime
    symbol: Symbol
    action: DecisionAction
    traded: bool
    code: str = Field(min_length=1, max_length=50)
    reason: str = Field(min_length=1)
    strategy: str | None = Field(default=None, max_length=100)
    confidence: float | None = Field(default=None, ge=0, le=1)
    signal_id: str | None = Field(default=None, max_length=64)
    client_order_id: str | None = Field(default=None, max_length=CLIENT_ORDER_ID_MAX)
    signal: Signal | None = None
    # parecer extra (regime, scores, ai_opinion) gravado junto do sinal em `decisions.signal` (JSON livre)
    insight: dict[str, Any] | None = None
    context: dict[str, Any] = Field(default_factory=dict)

    def decision_key(self, run_id: str) -> str:
        """Chave deterministica por run: reprocessar a mesma vela nao duplica a decisao."""
        raw = "|".join(
            [
                run_id,
                self.symbol,
                self.decided_at.isoformat(),
                self.action.value,
                self.strategy or "",
                self.code,
                self.signal_id or "",
            ]
        )
        return sha256(raw.encode()).hexdigest()[:48]
