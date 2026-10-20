from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from engine.domain.trade import Trade
from engine.infrastructure.persistence.models import TradeRow
from engine.infrastructure.persistence.serialization import ConflictError, affected, quantize, require_utc


class TradeConflictError(ConflictError):
    """Ja existe um trade com este `trade_id` e conteudo diferente; o ledger nao e reescrito."""


def _values(trade: Trade) -> dict[str, object]:
    return {
        "symbol": trade.symbol,
        "quantity": quantize(trade.quantity),
        "entry_price": quantize(trade.entry_price),
        "exit_price": quantize(trade.exit_price),
        "entry_fee": quantize(trade.entry_fee),
        "exit_fee": quantize(trade.exit_fee),
        "pnl": quantize(trade.pnl),
    }


def _trade(row: TradeRow) -> Trade:
    return Trade.model_validate(row, from_attributes=True)


class TradeRepository:
    """Somente `record` e leituras: o ledger e append-only (e o banco recusa UPDATE/DELETE)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(
        self,
        run_id: str,
        trade: Trade,
        *,
        strategy: str | None = None,
        risk_amount: Decimal | None = None,
    ) -> bool:
        """Idempotente por `trade_id`: True se gravou, False se ja existia identico (replay apos queda)."""
        if trade.trade_id is None:
            raise ValueError("trade_id is required to persist a trade")
        risk = None if risk_amount is None else quantize(risk_amount)
        r_multiple = (
            quantize(trade.pnl / risk_amount) if risk_amount is not None and risk_amount > 0 else None
        )
        context = {
            "opened_at": trade.opened_at,
            "closed_at": trade.closed_at,
            "exit_reason": trade.exit_reason.value,
            "strategy": strategy,
            "risk_amount": risk,
            "r_multiple": r_multiple,
        }
        stmt = (
            insert(TradeRow)
            .values(
                trade_id=trade.trade_id,
                run_id=run_id,
                **context,
                **_values(trade),
            )
            .on_conflict_do_nothing(index_elements=[TradeRow.run_id, TradeRow.trade_id])
        )
        if affected(await self._session.execute(stmt)) == 1:
            return True
        stored = await self._session.scalar(
            select(TradeRow).where(TradeRow.run_id == run_id, TradeRow.trade_id == trade.trade_id)
        )
        expected = {**_values(trade), **context}
        if stored is None or any(getattr(stored, name) != value for name, value in expected.items()):
            raise TradeConflictError(f"trade {trade.trade_id} ja registrado com conteudo diferente")
        return False

    async def list_for_run(
        self, run_id: str, *, since: datetime | None = None, until: datetime | None = None
    ) -> list[Trade]:
        """Trades fechados em [since, until), por ordem de fechamento."""
        stmt = select(TradeRow).where(TradeRow.run_id == run_id)
        if since is not None:
            stmt = stmt.where(TradeRow.closed_at >= require_utc(since, "since"))
        if until is not None:
            stmt = stmt.where(TradeRow.closed_at < require_utc(until, "until"))
        return [
            _trade(row) for row in await self._session.scalars(stmt.order_by(TradeRow.closed_at, TradeRow.id))
        ]
