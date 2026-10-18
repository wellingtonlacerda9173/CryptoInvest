from typing import Any

from pydantic_core import to_jsonable_python
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from engine.infrastructure.persistence.models import AuditLogRow


class AuditRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def append(
        self, actor: str, action: str, entity: str, payload: dict[str, Any] | None = None
    ) -> int:
        row = AuditLogRow(
            actor=actor, action=action, entity=entity, payload=to_jsonable_python(payload or {})
        )
        async with self._sessions.begin() as session:
            session.add(row)
            await session.flush()
            return row.id
