from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from engine.domain.journal import DecisionEntry
from engine.domain.signal import Signal
from engine.infrastructure.persistence.models import DecisionRow
from engine.infrastructure.persistence.serialization import ConflictError, affected, dump_model, to_json


class DecisionConflictError(ConflictError):
    """Ja existe uma decisao com esta chave e conteudo diferente; o feed nao e reescrito."""


_SIGNAL_FIELDS = frozenset(Signal.model_fields)


def _split_signal(stored: dict[str, Any] | None) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """`decisions.signal` guarda o sinal e (opcionalmente) o parecer do seletor lado a lado."""
    if not stored:
        return None, None
    levels = {k: v for k, v in stored.items() if k in _SIGNAL_FIELDS}
    insight = {k: v for k, v in stored.items() if k not in _SIGNAL_FIELDS}
    return levels or None, insight or None


def _merged_signal(entry: DecisionEntry) -> dict[str, Any] | None:
    if entry.signal is None and entry.insight is None:
        return None
    return {**(dump_model(entry.signal) if entry.signal else {}), **(to_json(entry.insight) or {})}


def _entry(row: DecisionRow) -> DecisionEntry:
    levels, insight = _split_signal(row.signal)
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
            "signal": levels,
            "insight": insight,
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
            "signal": _merged_signal(entry),
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
