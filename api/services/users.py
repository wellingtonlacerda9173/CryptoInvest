import hmac
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from api.audit import AuditAction, AuditResult, AuditService, RequestMeta
from api.container import Container
from api.errors import ApiError, BadRequest, Conflict, NotFound
from api.persistence.models import RecoveryCodeRow, UserRow
from api.security.passwords import password_problems
from api.security.tokens import hash_activation_token, new_activation_token
from api.services.sessions import SessionService

USERNAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9._@-]{2,63}")


class WeakPassword(ApiError):
    status, slug, title = 422, "weak-password", "Senha fraca"


def normalize_username(raw: str) -> str:
    """Minusculas ASCII. Texto nao ASCII NUNCA vira um nome valido (Kelvin U+212A vira 'k' em `lower()`,
    letras largas viram ASCII em NFKC): o nome volta como veio e falha em `USERNAME_PATTERN`."""
    name = raw.strip()
    return name.casefold() if name.isascii() else name


def is_valid_username(name: str) -> bool:
    return USERNAME_PATTERN.fullmatch(name) is not None


def totp_aad(user_id: uuid.UUID) -> str:
    return f"users:{user_id}:totp_secret"


def target_of(user_id: uuid.UUID) -> str:
    return f"user:{user_id}"


@dataclass(frozen=True, slots=True)
class Activation:
    token: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class CreatedUser:
    user: UserRow
    activation: Activation


def activation_matches(user: UserRow, token: str, now: datetime) -> bool:
    expected, expires = user.activation_token_hash, user.activation_expires_at
    if not expected or expires is None or expires <= now:
        return False
    return hmac.compare_digest(expected.encode(), hash_activation_token(token).encode())


class UserService:
    def __init__(self, container: Container, audit: AuditService) -> None:
        self._c = container
        self._audit = audit
        self._sessions = SessionService(container)

    def _issue_activation(self, user: UserRow, now: datetime) -> Activation:
        token = new_activation_token()
        expires = now + timedelta(hours=self._c.settings.activation_token_hours)
        user.activation_token_hash = hash_activation_token(token)
        user.activation_expires_at = expires
        return Activation(token, expires)

    async def _audit_issued(
        self,
        db: AsyncSession,
        user: UserRow,
        activation: Activation,
        *,
        actor: str,
        reason: str,
        meta: RequestMeta,
    ) -> None:
        await self._audit.record(
            actor=actor,
            action=AuditAction.ACTIVATION_ISSUED,
            target=target_of(user.id),
            result=AuditResult.SUCCESS,
            meta=meta,
            details={"expires_at": activation.expires_at, "reason": reason},
            session=db,
        )

    async def create(
        self, *, username: str, password: str, role: str, actor: str, meta: RequestMeta
    ) -> CreatedUser:
        name = normalize_username(username)
        if not is_valid_username(name):
            raise BadRequest(
                "Nome de usuario invalido: use 3 a 64 de a-z, 0-9, ponto, hifen, sublinhado ou arroba."
            )
        if problems := password_problems(password, username=name):
            raise WeakPassword(" ".join(problems))
        async with self._c.hash_gate.slot():
            hashed = await self._c.passwords.hash(password)
        now = self._c.clock()
        user = UserRow(
            id=uuid.uuid4(),
            username=name,
            password_hash=hashed,
            role=role,
            totp_enabled=False,
            failed_attempts=0,
            disabled=False,
            created_at=now,
            password_changed_at=now,
        )
        activation = self._issue_activation(user, now)
        try:
            async with self._c.sessions.begin() as db:
                db.add(user)
                await db.flush()
                await self._audit.record(
                    actor=actor,
                    action=AuditAction.USER_CREATED,
                    target=target_of(user.id),
                    result=AuditResult.SUCCESS,
                    meta=meta,
                    details={"username": name, "role": role},
                    session=db,
                )
                await self._audit_issued(db, user, activation, actor=actor, reason="created", meta=meta)
        except IntegrityError:
            raise Conflict("Ja existe um usuario com esse nome.") from None
        return CreatedUser(user, activation)

    async def list(self) -> list[UserRow]:
        async with self._c.sessions.begin() as db:
            return list(
                (await db.scalars(select(UserRow).order_by(UserRow.created_at, UserRow.username))).all()
            )

    async def _lock_active_admins(self, db: AsyncSession, target_id: uuid.UUID) -> None:
        """Trava os admins ativos E o alvo, numa unica instrucao em ordem fixa por id (sem deadlock).

        Serializa rebaixamentos e desabilitacoes: duas transacoes cruzadas nao veem mais "outro admin". O alvo
        entra na mesma varredura: travas em ordem de id, como a de `reencrypt_all`, nunca em ordem inversa."""
        await db.scalars(
            select(UserRow.id)
            .where(or_(and_(UserRow.role == "admin", UserRow.disabled.is_(False)), UserRow.id == target_id))
            .order_by(UserRow.id)
            .with_for_update()
        )

    async def _active_admins(self, db: AsyncSession) -> int:
        count = await db.scalar(
            select(func.count())
            .select_from(UserRow)
            .where(UserRow.role == "admin", UserRow.disabled.is_(False))
        )
        return int(count or 0)

    async def update(
        self,
        *,
        user_id: uuid.UUID,
        role: str | None,
        disabled: bool | None,
        actor_id: uuid.UUID,
        actor: str,
        meta: RequestMeta,
    ) -> UserRow:
        async with self._c.sessions.begin() as db:
            await self._lock_active_admins(db, user_id)
            user = await db.get(UserRow, user_id, with_for_update=True)
            if user is None:
                raise NotFound("Usuario nao encontrado.")
            changes: dict[str, Any] = {}
            if role is not None and role != user.role:
                changes["role"] = {"from": user.role, "to": role}
            if disabled is not None and disabled != user.disabled:
                changes["disabled"] = {"from": user.disabled, "to": disabled}
            loses_admin = (
                user.role == "admin" and not user.disabled and (role == "reader" or disabled is True)
            )
            if loses_admin:
                if user.id == actor_id:
                    raise Conflict("Voce nao pode rebaixar nem desabilitar a propria conta.")
                if await self._active_admins(db) <= 1:
                    raise Conflict("Deve restar ao menos um administrador ativo.")
            if changes:
                if role is not None:
                    user.role = role
                if disabled is not None:
                    user.disabled = disabled
                if "role" in changes or disabled is True:
                    await self._sessions.revoke_user_sessions(db, user.id, "user_changed")
                await self._audit.record(
                    actor=actor,
                    action=AuditAction.USER_UPDATED,
                    target=target_of(user.id),
                    result=AuditResult.SUCCESS,
                    meta=meta,
                    details=changes,
                    session=db,
                )
            return user

    async def unlock(
        self, *, user_id: uuid.UUID, actor: str, meta: RequestMeta, via: str | None = None
    ) -> UserRow:
        return await self._unlock(UserRow.id == user_id, actor=actor, meta=meta, via=via)

    async def unlock_username(
        self, *, username: str, actor: str, meta: RequestMeta, via: str | None = None
    ) -> UserRow:
        return await self._unlock(
            UserRow.username == normalize_username(username), actor=actor, meta=meta, via=via
        )

    async def _unlock(
        self, condition: ColumnElement[bool], *, actor: str, meta: RequestMeta, via: str | None
    ) -> UserRow:
        """Zera o bloqueio duro do banco e incrementa `lock_epoch`: o bloqueio de senha vive na memoria de
        cada processo da API, que o solta ao ver a epoca mudar (CLI e outros workers); aqui, na hora."""
        async with self._c.sessions.begin() as db:
            user = await db.scalar(select(UserRow).where(condition).with_for_update())
            if user is None:
                raise NotFound("Usuario nao encontrado.")
            user.failed_attempts = 0
            user.locked_until = None
            user.lock_epoch += 1
            await self._audit.record(
                actor=actor,
                action=AuditAction.USER_UNLOCKED,
                target=target_of(user.id),
                result=AuditResult.SUCCESS,
                meta=meta,
                details={"via": via} if via else None,
                session=db,
            )
        tag = self._c.user_tag(user.username)
        self._c.user_throttle.clear_prefix(f"p:{tag}|")
        self._c.user_pressure.clear(tag)
        self._c.lock_epochs.changed(tag, user.lock_epoch)
        return user

    async def reset_two_factor(
        self, *, user_id: uuid.UUID, actor_id: uuid.UUID, actor: str, meta: RequestMeta
    ) -> Activation:
        if user_id == actor_id:
            raise Conflict("Voce nao pode redefinir o proprio 2FA; peca a outro administrador.")
        async with self._c.sessions.begin() as db:
            user = await db.get(UserRow, user_id, with_for_update=True)
            if user is None:
                raise NotFound("Usuario nao encontrado.")
            user.totp_secret = None
            user.totp_enabled = False
            user.totp_last_counter = None
            await db.execute(delete(RecoveryCodeRow).where(RecoveryCodeRow.user_id == user.id))
            await self._sessions.revoke_user_sessions(db, user.id, "two_factor_reset")
            activation = self._issue_activation(user, self._c.clock())
            await self._audit.record(
                actor=actor,
                action=AuditAction.USER_TWO_FACTOR_RESET,
                target=target_of(user.id),
                result=AuditResult.SUCCESS,
                meta=meta,
                session=db,
            )
            await self._audit_issued(db, user, activation, actor=actor, reason="two_factor_reset", meta=meta)
        return activation

    async def reissue_activation(self, *, username: str, actor: str, meta: RequestMeta) -> Activation:
        """Novo token para quem ainda nao tem 2FA (ex.: conta anterior a este controle). Uso: CLI."""
        async with self._c.sessions.begin() as db:
            user = await db.scalar(
                select(UserRow).where(UserRow.username == normalize_username(username)).with_for_update()
            )
            if user is None:
                raise NotFound("Usuario nao encontrado.")
            if user.totp_enabled:
                raise Conflict("O usuario ja tem 2FA; use a redefinicao de 2FA.")
            activation = self._issue_activation(user, self._c.clock())
            await self._audit_issued(db, user, activation, actor=actor, reason="reissued", meta=meta)
        return activation


def user_locked_until(user: UserRow, now: datetime) -> datetime | None:
    return user.locked_until if user.locked_until and user.locked_until > now else None
