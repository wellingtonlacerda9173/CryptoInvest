from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from engine.domain.journal import DecisionEntry
from engine.infrastructure.persistence.models import DecisionRow
from engine.infrastructure.persistence.serialization import ConflictError, affected, dump_model, to_json


class DecisionConflictError(ConflictError):
    """Ja existe uma decisao com esta chave e conteudo diferente; o feed nao e reescrito."""


def _entry(row: DecisionRow) -> DecisionEntry:
    return DecisionEntry.model_validate(
        {
            "decided_at": row.decided_at,
            "symbol": row.symbol,
            "action": row.action,
            "traded": row.traded,
            "code": row.code,
            "reason": row.reason,
            "strategy": row.strategy,
            "confidence": row.confidence,
            "signal_id": row.signal_id,
            "client_order_id": row.client_order_id,
            "signal": row.signal,
            "context": row.context,
        }
    )


class DecisionRepository:
    """Feed append-only: so `record` e leituras."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(self, run_id: str, entry: DecisionEntry) -> bool:
        """Idempotente por (run, chave): False se ja existia identica; conteudo diferente e conflito."""
        key = entry.decision_key(run_id)
        values = {
            "decided_at": entry.decided_at,
            "symbol": entry.symbol,
            "action": entry.action.value,
            "traded": entry.traded,
            "code": entry.code,
            "reason": entry.reason,
            "strategy": entry.strategy,
            "confidence": entry.confidence,
            "signal_id": entry.signal_id,
            "client_order_id": entry.client_order_id,
            "signal": dump_model(entry.signal) if entry.signal else None,
            "context": to_json(entry.context),
        }
        stmt = (
            insert(DecisionRow)
            .values(decision_key=key, run_id=run_id, **values)
            .on_conflict_do_nothing(index_elements=[DecisionRow.run_id, DecisionRow.decision_key])
        )
        if affected(await self._session.execute(stmt)) == 1:
            return True
        stored = await self._session.scalar(
            select(DecisionRow).where(DecisionRow.run_id == run_id, DecisionRow.decision_key == key)
        )
        if stored is None or any(getattr(stored, name) != value for name, value in values.items()):
            raise DecisionConflictError(f"decisao {key} ja registrada com conteudo diferente")
        return False

    async def list_recent(self, run_id: str, *, limit: int = 100) -> list[DecisionEntry]:
        stmt = (
            select(DecisionRow)
            .where(DecisionRow.run_id == run_id)
            .order_by(DecisionRow.decided_at.desc(), DecisionRow.id.desc())
            .limit(limit)
        )
        return [_entry(row) for row in await self._session.scalars(stmt)]

    async def list_for_signal(self, run_id: str, signal_id: str) -> list[DecisionEntry]:
        stmt = (
            select(DecisionRow)
            .where(DecisionRow.run_id == run_id, DecisionRow.signal_id == signal_id)
            .order_by(DecisionRow.id)
        )
        return [_entry(row) for row in await self._session.scalars(stmt)]
