from datetime import datetime
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from engine.application.risk import ManagedPosition
from engine.infrastructure.persistence.models import PositionRow
from engine.infrastructure.persistence.serialization import PersistenceError, affected, quantize, require_utc

_FIELDS = (
    "symbol",
    "entry_price",
    "initial_quantity",
    "quantity",
    "initial_stop",
    "stop",
    "take_profit",
    "highest_price",
    "entry_fee",
    "breakeven_reached",
    "partial_done",
    "realized_pnl",
    "opened_at",
)
# entrada, stop inicial, taxa e abertura sao fixos durante a vida da posicao
_FIXED = ("symbol", "entry_price", "initial_quantity", "initial_stop", "entry_fee", "opened_at")
_MUTABLE = (
    "quantity",
    "stop",
    "take_profit",
    "highest_price",
    "breakeven_reached",
    "partial_done",
    "realized_pnl",
)


def _position(row: PositionRow) -> ManagedPosition:
    return ManagedPosition.model_validate({name: getattr(row, name) for name in _FIELDS})


def _q(position: ManagedPosition) -> ManagedPosition:
    """Mesma escala do banco, para comparar o que entrou com o que foi gravado."""
    return position.model_copy(
        update={
            name: quantize(value) for name in _FIELDS if isinstance(value := getattr(position, name), Decimal)
        }
    )


def check_position_transition(stored: ManagedPosition, new: ManagedPosition) -> None:
    """Uma posicao aberta so evolui para frente; um replay antigo nao pode sobrescrever o estado atual.

    Stop, maxima e flags so avancam; a quantidade so diminui; o alvo, uma vez removido, nao volta; e o PnL
    realizado so muda junto com uma reducao de quantidade (saida parcial).
    """
    symbol = new.symbol
    for name in _FIXED:
        if getattr(stored, name) != getattr(new, name):
            raise PersistenceError(f"{symbol}: {name} nao pode mudar em posicao aberta")
    if new.stop < stored.stop:
        raise PersistenceError(f"{symbol}: stop nao pode ser reduzido em posicao aberta")
    if new.quantity > stored.quantity:
        raise PersistenceError(f"{symbol}: quantity nao pode aumentar em posicao aberta")
    if new.highest_price < stored.highest_price:
        raise PersistenceError(f"{symbol}: highest_price nao pode regredir em posicao aberta")
    for flag in ("partial_done", "breakeven_reached"):
        if getattr(stored, flag) and not getattr(new, flag):
            raise PersistenceError(f"{symbol}: {flag} nao pode regredir em posicao aberta")
    if stored.take_profit is None and new.take_profit is not None:
        raise PersistenceError(f"{symbol}: take_profit removido nao pode voltar em posicao aberta")
    if new.quantity == stored.quantity and new.realized_pnl != stored.realized_pnl:
        raise PersistenceError(f"{symbol}: realized_pnl so muda com saida parcial (quantity menor)")


class PositionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save_open(self, run_id: str, position: ManagedPosition) -> None:
        """Abre a posicao do simbolo ou a atualiza; regressoes violam `check_position_transition`."""
        new = _q(position)
        row = await self._session.scalar(
            select(PositionRow)
            .where(
                PositionRow.run_id == run_id,
                PositionRow.symbol == position.symbol,
                PositionRow.closed_at.is_(None),
            )
            .with_for_update()
        )
        if row is None:
            values = {"run_id": run_id, **{name: getattr(new, name) for name in _FIELDS}}
            await self._session.execute(insert(PositionRow).values(values))
            return
        check_position_transition(_position(row), new)
        await self._session.execute(
            update(PositionRow).where(PositionRow.id == row.id).values({n: getattr(new, n) for n in _MUTABLE})
        )

    async def close(self, run_id: str, symbol: str, closed_at: datetime) -> bool:
        stmt = (
            update(PositionRow)
            .where(
                PositionRow.run_id == run_id, PositionRow.symbol == symbol, PositionRow.closed_at.is_(None)
            )
            .values(closed_at=require_utc(closed_at, "closed_at"))
        )
        return affected(await self._session.execute(stmt)) == 1

    async def list_open(self, run_id: str) -> list[ManagedPosition]:
        stmt = (
            select(PositionRow)
            .where(PositionRow.run_id == run_id, PositionRow.closed_at.is_(None))
            .order_by(PositionRow.id)
        )
        return [_position(row) for row in await self._session.scalars(stmt)]
