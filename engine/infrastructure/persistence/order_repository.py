from decimal import Decimal
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from engine.domain.journal import FillRecord, OrderRecord
from engine.domain.order import OrderStatus
from engine.infrastructure.persistence.models import FillRow, OrderRow
from engine.infrastructure.persistence.serialization import ConflictError, affected, quantize

OPEN_STATUSES = (OrderStatus.NEW.value, OrderStatus.PARTIALLY_FILLED.value)

# NEW < PARTIALLY_FILLED < estados terminais (que nunca mudam nem regridem)
_RANK = {
    OrderStatus.NEW: 0,
    OrderStatus.PARTIALLY_FILLED: 1,
    OrderStatus.FILLED: 2,
    OrderStatus.CANCELED: 2,
    OrderStatus.REJECTED: 2,
    OrderStatus.EXPIRED: 2,
}
_IMMUTABLE = ("symbol", "side", "type", "quantity", "price", "stop_loss", "take_profit", "created_at")


class OrderConflictError(ConflictError):
    """Atualizacao de ordem incompativel com o que ja foi gravado (campos fixos, terminal, fill menor)."""


class FillConflictError(ConflictError):
    """Ja existe um fill com este `fill_id` e conteudo diferente."""


def _order(row: OrderRow) -> OrderRecord:
    return OrderRecord.model_validate(row, from_attributes=True)


def _norm(value: Any) -> Any:
    return quantize(value) if isinstance(value, Decimal) else value


def next_order_state(stored: OrderRecord, incoming: OrderRecord) -> OrderRecord | None:
    """Estado a gravar sobre `stored`; None se `incoming` e um replay antigo (ignorado).

    Avanca por ranking de status, nunca por relogio: um terminal nao muda, `filled_quantity` nao diminui e
    os campos fixos da ordem nao divergem. No mesmo ranking nao terminal, `updated_at` so desempata: mais
    `filled_quantity` ou um `exchange_order_id` novo avancam mesmo com relogio atrasado. Violacoes levantam
    `OrderConflictError`.
    """
    for name in _IMMUTABLE:
        if _norm(getattr(stored, name)) != _norm(getattr(incoming, name)):
            raise OrderConflictError(f"ordem {stored.client_order_id}: {name} nao pode mudar")
    old, new = _RANK[stored.status], _RANK[incoming.status]
    if new < old:
        return None
    if new == old < 2 and incoming.updated_at < stored.updated_at:
        more_filled = quantize(incoming.filled_quantity) > quantize(stored.filled_quantity)
        new_exchange_id = bool(incoming.exchange_order_id) and not stored.exchange_order_id
        if not (more_filled or new_exchange_id):
            return None
        incoming = incoming.model_copy(update={"updated_at": stored.updated_at})
    if quantize(incoming.filled_quantity) < quantize(stored.filled_quantity):
        raise OrderConflictError(f"ordem {stored.client_order_id}: filled_quantity nao pode diminuir")
    if old == 2:
        if stored.status is not incoming.status or quantize(incoming.filled_quantity) != quantize(
            stored.filled_quantity
        ):
            raise OrderConflictError(
                f"ordem {stored.client_order_id}: estado terminal {stored.status.value} nao pode mudar"
            )
        return None
    return incoming


class OrderRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(self, run_id: str, order: OrderRecord) -> None:
        """Cria a ordem do run ou avanca seu estado (ver `next_order_state`); replay antigo e no-op."""
        row = await self._session.scalar(
            select(OrderRow)
            .where(OrderRow.run_id == run_id, OrderRow.client_order_id == order.client_order_id)
            .with_for_update()
        )
        if row is None:
            await self._session.execute(insert(OrderRow).values(run_id=run_id, **order.model_dump()))
            return
        if (state := next_order_state(_order(row), order)) is None:
            return
        await self._session.execute(
            update(OrderRow)
            .where(OrderRow.id == row.id)
            .values(
                status=state.status.value,
                exchange_order_id=state.exchange_order_id or row.exchange_order_id,
                filled_quantity=state.filled_quantity,
                average_price=state.average_price or row.average_price,
                updated_at=state.updated_at,
            )
        )

    async def get(self, run_id: str, client_order_id: str) -> OrderRecord | None:
        row = await self._session.scalar(
            select(OrderRow).where(OrderRow.run_id == run_id, OrderRow.client_order_id == client_order_id)
        )
        return _order(row) if row else None

    async def list_open(self, run_id: str) -> list[OrderRecord]:
        stmt = (
            select(OrderRow)
            .where(OrderRow.run_id == run_id, OrderRow.status.in_(OPEN_STATUSES))
            .order_by(OrderRow.id)
        )
        return [_order(row) for row in await self._session.scalars(stmt)]


class FillRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(self, run_id: str, fill: FillRecord) -> bool:
        """Idempotente por (run, `fill_id`): False se ja existia identico; conteudo diferente e conflito."""
        values = fill.model_dump(mode="python")
        stmt = (
            insert(FillRow)
            .values(run_id=run_id, **values)
            .on_conflict_do_nothing(index_elements=[FillRow.run_id, FillRow.fill_id])
        )
        if affected(await self._session.execute(stmt)) == 1:
            return True
        stored = await self._session.scalar(
            select(FillRow).where(FillRow.run_id == run_id, FillRow.fill_id == fill.fill_id)
        )
        if stored is None or any(
            _norm(getattr(stored, name)) != _norm(value) for name, value in values.items()
        ):
            raise FillConflictError(f"fill {fill.fill_id} ja registrado com conteudo diferente")
        return False

    async def list_for_run(self, run_id: str) -> list[FillRecord]:
        stmt = select(FillRow).where(FillRow.run_id == run_id).order_by(FillRow.filled_at, FillRow.id)
        return [
            FillRecord.model_validate(row, from_attributes=True) for row in await self._session.scalars(stmt)
        ]
