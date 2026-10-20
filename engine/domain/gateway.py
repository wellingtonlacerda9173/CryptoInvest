from collections.abc import AsyncIterator
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from engine.domain.candle import Candle
from engine.domain.market import Balance, OrderBookSnapshot
from engine.domain.order import Order, OrderReport
from engine.domain.types import Symbol, Timeframe


class MarketDataGateway(Protocol):
    async def fetch_candles(
        self, symbol: Symbol, timeframe: Timeframe, start: datetime, end: datetime | None = None
    ) -> list[Candle]: ...

    def stream_candles(
        self, symbol: Symbol, timeframe: Timeframe, since: datetime | None = None
    ) -> AsyncIterator[Candle]:
        """Emite somente velas fechadas; com `since`, recupera as velas fechadas desde esse instante."""
        ...

    def stream_order_book(self, symbol: Symbol, depth: int = 10) -> AsyncIterator[OrderBookSnapshot]: ...


class ExchangeGateway(MarketDataGateway, Protocol):
    """Contrato unico dos gateways Backtest/Paper/Live (implementacoes em fases futuras)."""

    async def get_balances(self) -> dict[str, Balance]: ...

    async def place_order(self, order: Order) -> OrderReport:
        """Implementacoes devem chamar `revalidate_order(order)` antes de enviar a ordem."""
        ...

    async def cancel_order(self, symbol: Symbol, client_order_id: str) -> OrderReport: ...

    async def get_order(self, symbol: Symbol, client_order_id: str) -> OrderReport: ...


class DuplicateOrderError(ValueError):
    """`client_order_id` ja usado neste gateway."""


class UnknownOrderError(KeyError):
    """Ordem (ou OCO) inexistente ou nao mais ativa."""


class ProtectedOrderGateway(ExchangeGateway, Protocol):
    """Gateway que mantem o OCO (stop + alvo) de cada entrada e deixa a gestao da posicao ajusta-lo."""

    def raise_stop(self, client_order_id: str, new_stop: Decimal) -> OrderReport:
        """Sobe o stop do OCO da entrada; nunca desce e nunca alcanca o alvo nem o preco corrente."""
        ...

    def drop_target(self, client_order_id: str) -> OrderReport:
        """Transforma o OCO em stop puro: o alvo passa a ser gerido por ordens manuais (parcial)."""
        ...
