from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Query
from sqlalchemy import select

from api.deps import AdminDep, ContainerDep
from api.errors import PROBLEM_RESPONSES
from api.schemas import AuditEntryOut, AuditPage
from engine.infrastructure.persistence.models import AuditLogRow

router = APIRouter(prefix="/audit", tags=["audit"], responses=PROBLEM_RESPONSES)


def to_entry(row: AuditLogRow) -> AuditEntryOut:
    payload: dict[str, Any] = row.payload or {}
    result = payload.get("result")
    return AuditEntryOut(
        id=row.id,
        occurred_at=row.occurred_at,
        actor=row.actor,
        action=row.action,
        target=row.entity,
        result=cast(
            "Literal['success', 'failure', 'denied'] | None",
            result if result in {"success", "failure", "denied"} else None,
        ),
        ip=payload.get("ip"),
        user_agent=payload.get("user_agent"),
        request_id=payload.get("request_id"),
        details=payload.get("details") or {},
    )


@router.get(
    "",
    operation_id="listAuditLog",
    response_model=AuditPage,
    summary="Trilha de auditoria, da mais recente para a mais antiga (admin)",
)
async def list_audit(
    _: AdminDep,
    container: ContainerDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: Annotated[int | None, Query(ge=1, description="`next_cursor` da pagina anterior")] = None,
    actor: Annotated[str | None, Query(max_length=100)] = None,
    action: Annotated[str | None, Query(max_length=100, description="Prefixo, ex.: `auth.`")] = None,
) -> AuditPage:
    stmt = select(AuditLogRow).order_by(AuditLogRow.id.desc()).limit(limit + 1)
    if cursor is not None:
        stmt = stmt.where(AuditLogRow.id < cursor)
    if actor:
        stmt = stmt.where(AuditLogRow.actor == actor)
    if action:
        stmt = stmt.where(AuditLogRow.action.startswith(action, autoescape=True))
    async with container.sessions() as db:
        rows = list((await db.scalars(stmt)).all())
    page = rows[:limit]
    return AuditPage(
        items=[to_entry(r) for r in page], next_cursor=page[-1].id if len(rows) > limit else None
    )
