import uuid

from fastapi import APIRouter
from sqlalchemy import select

from api.audit import AuditAction, AuditResult
from api.deps import AuditDep, ContainerDep, MetaDep, UserDep
from api.errors import PROBLEM_RESPONSES, NotFound
from api.persistence.models import SessionRow
from api.schemas import SessionOut
from api.services.mapping import session_out
from api.services.sessions import SessionService

router = APIRouter(prefix="/sessions", tags=["sessions"], responses=PROBLEM_RESPONSES)


@router.get(
    "", operation_id="listSessions", response_model=list[SessionOut], summary="Sessoes ativas do usuario"
)
async def list_sessions(principal: UserDep, container: ContainerDep) -> list[SessionOut]:
    rows = await SessionService(container).list_for_user(principal.user_id)
    return [session_out(row, current=row.id == principal.session_id) for row in rows]


@router.delete(
    "/{session_id}", operation_id="revokeSession", status_code=204, summary="Revoga uma sessao propria"
)
async def revoke_session(
    session_id: uuid.UUID, principal: UserDep, container: ContainerDep, audit: AuditDep, meta: MetaDep
) -> None:
    async with container.sessions.begin() as db:
        owned = await db.scalar(
            select(SessionRow.id).where(SessionRow.id == session_id, SessionRow.user_id == principal.user_id)
        )
        if owned is None:
            await audit.record(
                actor=principal.username,
                action=AuditAction.ACCESS_DENIED,
                target=f"session:{session_id}",
                result=AuditResult.DENIED,
                meta=meta,
                details={"reason": "session_not_owned"},
            )
            raise NotFound("Sessao nao encontrada.")
        await SessionService(container).revoke(db, session_id, "user_revoked")
        await audit.record(
            actor=principal.username,
            action=AuditAction.SESSION_REVOKED,
            target=f"session:{session_id}",
            result=AuditResult.SUCCESS,
            meta=meta,
            session=db,
        )


@router.delete(
    "", operation_id="revokeOtherSessions", status_code=204, summary="Revoga todas as outras sessoes"
)
async def revoke_other_sessions(
    principal: UserDep, container: ContainerDep, audit: AuditDep, meta: MetaDep
) -> None:
    async with container.sessions.begin() as db:
        await SessionService(container).revoke_user_sessions(
            db, principal.user_id, "user_revoked_others", keep=principal.session_id
        )
        await audit.record(
            actor=principal.username,
            action=AuditAction.SESSION_REVOKED,
            target=f"user:{principal.user_id}",
            result=AuditResult.SUCCESS,
            meta=meta,
            details={"scope": "all_others"},
            session=db,
        )
