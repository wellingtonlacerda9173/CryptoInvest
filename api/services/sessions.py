import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from api.audit import RequestMeta
from api.container import Container
from api.persistence.models import SessionRow, UserRow
from api.security.tokens import hash_token, new_session_token

TOUCH_INTERVAL = timedelta(seconds=60)
PURGE_AFTER = timedelta(days=7)


@dataclass(frozen=True, slots=True)
class Principal:
    session_id: uuid.UUID
    user_id: uuid.UUID
    username: str
    role: str
    totp_enabled: bool
    fresh_auth_at: datetime | None
    mfa_verified: bool


class SessionService:
    def __init__(self, container: Container) -> None:
        self._c = container

    def new_row(
        self, user_id: uuid.UUID, now: datetime, meta: RequestMeta, *, verified: bool
    ) -> tuple[str, SessionRow]:
        s = self._c.settings
        token = new_session_token()
        if verified:
            absolute = now + timedelta(hours=s.session_absolute_hours)
            expires = min(now + timedelta(minutes=s.session_idle_minutes), absolute)
        else:
            absolute = expires = now + timedelta(minutes=s.pending_login_minutes)
        row = SessionRow(
            id=uuid.uuid4(),
            user_id=user_id,
            token_hash=hash_token(token),
            created_at=now,
            last_seen_at=now,
            expires_at=expires,
            absolute_expires_at=absolute,
            mfa_verified_at=now if verified else None,
            fresh_auth_at=now if verified else None,
            pending_failures=0,
            ip=meta.ip,
            user_agent=(meta.user_agent or "")[:200] or None,
        )
        return token, row

    async def authenticate(self, token: str | None, *, verified: bool) -> Principal | None:
        """Sessao valida: nao revogada, dentro das expiracoes absoluta e por inatividade, usuario ativo."""
        if not token or len(token) > 128:
            return None
        now = self._c.clock()
        async with self._c.sessions.begin() as db:
            found = (
                await db.execute(
                    select(SessionRow, UserRow)
                    .join(UserRow, UserRow.id == SessionRow.user_id)
                    .where(SessionRow.token_hash == hash_token(token))
                )
            ).first()
            if found is None:
                return None
            session, user = found
            alive = (
                session.revoked_at is None
                and now < session.expires_at
                and now < session.absolute_expires_at
                and not user.disabled
                and (session.mfa_verified_at is not None) == verified
            )
            if not alive:
                return None
            if verified and now - session.last_seen_at >= TOUCH_INTERVAL:
                idle = timedelta(minutes=self._c.settings.session_idle_minutes)
                session.last_seen_at = now
                session.expires_at = min(now + idle, session.absolute_expires_at)
            return Principal(
                session_id=session.id,
                user_id=user.id,
                username=user.username,
                role=user.role,
                totp_enabled=user.totp_enabled,
                fresh_auth_at=session.fresh_auth_at,
                mfa_verified=session.mfa_verified_at is not None,
            )

    async def revoke(self, db: AsyncSession, session_id: uuid.UUID, reason: str) -> None:
        await db.execute(
            update(SessionRow)
            .where(SessionRow.id == session_id, SessionRow.revoked_at.is_(None))
            .values(revoked_at=self._c.clock(), revoked_reason=reason)
        )

    async def revoke_user_sessions(
        self, db: AsyncSession, user_id: uuid.UUID, reason: str, *, keep: uuid.UUID | None = None
    ) -> None:
        stmt = (
            update(SessionRow)
            .where(SessionRow.user_id == user_id, SessionRow.revoked_at.is_(None))
            .values(revoked_at=self._c.clock(), revoked_reason=reason)
        )
        if keep is not None:
            stmt = stmt.where(SessionRow.id != keep)
        await db.execute(stmt)

    async def purge_old(self, db: AsyncSession) -> None:
        cutoff = self._c.clock() - PURGE_AFTER
        await db.execute(delete(SessionRow).where(SessionRow.absolute_expires_at < cutoff))

    async def list_for_user(self, user_id: uuid.UUID) -> list[SessionRow]:
        now = self._c.clock()
        async with self._c.sessions.begin() as db:
            rows = await db.scalars(
                select(SessionRow)
                .where(
                    SessionRow.user_id == user_id,
                    SessionRow.revoked_at.is_(None),
                    SessionRow.mfa_verified_at.is_not(None),
                    SessionRow.expires_at > now,
                    SessionRow.absolute_expires_at > now,
                )
                .order_by(SessionRow.created_at.desc())
            )
            return list(rows.all())
