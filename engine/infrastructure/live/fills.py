from collections import deque
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

import structlog
from pydantic import Field

from engine.domain.base import DomainModel
from engine.domain.types import Fee, Price, Quantity, Symbol, UtcDatetime

log = structlog.get_logger()
_FALLBACK_FEE_RATE = Decimal("0.001")


class FillKind(StrEnum):
    ENTRY = "entry"
    TARGET = "target"
    STOP = "stop"
    EXIT = "exit"
    EMERGENCY = "emergency"


class LiveFill(DomainModel):
    """Fill real (/myTrades). `commission` fica na moeda original (`commission_asset`, p.ex. BNB).

    `commission_quote` e o custo na moeda de cotacao: conversao exata (cotacao do momento) ou, quando
    `commission_estimated`, estimativa conservadora (taxa padrao sobre o valor negociado).
    """

    sequence: int = Field(ge=1)
    trade_id: int
    entry_id: str
    order_client_id: str
    symbol: Symbol
    side: str
    kind: FillKind
    quantity: Quantity
    price: Price
    quote_quantity: Decimal
    commission: Fee
    commission_asset: str
    commission_quote: Decimal
    commission_estimated: bool = False
    is_maker: bool
    filled_at: UtcDatetime


class FillLog:
    """Relatorio de fills append-only, idempotente por (simbolo, trade_id), com entrega ao sink via outbox.

    O fill so sai da outbox quando o sink confirma; falha do sink NAO perde nem duplica o fill e nunca
    interrompe o fluxo de protecao: fica pendente e e reentregue em `flush()` (chamado a cada `record`/ciclo).
    """

    def __init__(
        self,
        sink: Callable[[LiveFill], None] | None = None,
        on_error: Callable[[int], None] | None = None,
    ) -> None:
        self._fills: list[LiveFill] = []
        self._seen: set[tuple[str, int]] = set()
        self._outbox: deque[LiveFill] = deque()
        self._sink = sink
        self._on_error = on_error

    def __len__(self) -> int:
        return len(self._fills)

    @property
    def pending(self) -> int:
        return len(self._outbox)

    def all(self) -> tuple[LiveFill, ...]:
        return tuple(self._fills)

    def knows(self, symbol: str, trade_id: int) -> bool:
        return (symbol, trade_id) in self._seen

    def flush(self) -> bool:
        """Entrega a outbox em ordem; para na primeira falha. True = nada pendente."""
        while self._outbox:
            fill = self._outbox[0]
            if self._sink is not None:
                try:
                    self._sink(fill)
                except Exception as error:
                    log.error("live_fill_sink_failed", trade=fill.trade_id, error=type(error).__name__)
                    if self._on_error is not None:
                        self._on_error(len(self._outbox))
                    return False
            self._outbox.popleft()
        return True

    def record(
        self,
        trade: dict[str, Any],
        *,
        symbol: Symbol,
        entry_id: str,
        order_client_id: str,
        kind: FillKind,
        commission_quote: Decimal | None = None,
        commission_estimated: bool = False,
    ) -> LiveFill | None:
        key = (symbol, int(trade["id"]))
        if key in self._seen:
            self.flush()
            return None
        quantity, price = Decimal(trade["qty"]), Decimal(trade["price"])
        quote_quantity, commission = Decimal(trade["quoteQty"]), Decimal(trade["commission"])
        asset = str(trade["commissionAsset"])
        base, quote = symbol.split("/")
        estimated = commission_estimated
        if commission_quote is None:
            if commission == 0 or asset == quote:
                commission_quote = commission
            elif asset == base:
                commission_quote = commission * price
            else:
                commission_quote, estimated = quote_quantity * _FALLBACK_FEE_RATE, True
        fill = LiveFill(
            sequence=len(self._fills) + 1,
            trade_id=int(trade["id"]),
            entry_id=entry_id,
            order_client_id=order_client_id,
            symbol=symbol,
            side="BUY" if trade["isBuyer"] else "SELL",
            kind=kind,
            quantity=quantity,
            price=price,
            quote_quantity=quote_quantity,
            commission=commission,
            commission_asset=asset,
            commission_quote=commission_quote,
            commission_estimated=estimated,
            is_maker=bool(trade["isMaker"]),
            filled_at=_from_ms(int(trade["time"])),
        )
        self._seen.add(key)
        self._fills.append(fill)
        self._outbox.append(fill)
        self.flush()
        return fill


def _from_ms(value: int) -> datetime:
    from datetime import UTC

    return datetime.fromtimestamp(value / 1000, tz=UTC)
