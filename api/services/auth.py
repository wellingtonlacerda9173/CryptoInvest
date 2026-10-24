import asyncio
import base64
import io
import uuid
import weakref
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

import qrcode
import structlog
from qrcode.image.svg import SvgPathImage
from sqlalchemy import CursorResult, delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from api.audit import AuditAction, AuditResult, AuditService, RequestMeta
from api.container import Container
from api.errors import BadRequest, Conflict, TooManyRequests, Unauthorized
from api.persistence.models import RecoveryCodeRow, SessionRow, UserRow
from api.security import totp
from api.security.crypto import DecryptionError
from api.security.passwords import password_problems
from api.security.throttle import LOCKOUT_THRESHOLD, lockout_seconds, throttle_ip
from api.security.tokens import hash_recovery_code, new_recovery_codes
from api.services.sessions import Principal, SessionService
from api.services.users import (
    WeakPassword,
    activation_matches,
    is_valid_username,
    normalize_username,
    target_of,
    totp_aad,
)

log = structlog.get_logger()

MAX_PENDING_FAILURES = 5
GENERIC_LOGIN_ERROR = "Usuario, senha ou codigo nao conferem. Confira e tente de novo."


@dataclass(frozen=True, slots=True)
class Started:
    token: str
    session_id: uuid.UUID
    expires_at: datetime
    max_age_seconds: int


@dataclass(frozen=True, slots=True)
class LoginOutcome:
    started: Started
    stage: str


@dataclass(frozen=True, slots=True)
class EnrollmentInfo:
    uri: str
    secret: str
    qr_data_uri: str


@dataclass(frozen=True, slots=True)
class ActivatedSession:
    started: Started
    user: UserRow
    recovery_codes: list[str]
    recovery_code_used: bool


def qr_data_uri(content: str) -> str:
    image = qrcode.make(content, image_factory=SvgPathImage, border=2)
    buffer = io.BytesIO()
    image.save(buffer)
    return "data:image/svg+xml;base64," + base64.b64encode(buffer.getvalue()).decode()


_USER_LOCKS: weakref.WeakValueDictionary[uuid.UUID, asyncio.Lock] = weakref.WeakValueDictionary()


def _rowcount(result: Any) -> int:
    return int(cast("CursorResult[Any]", result).rowcount)


class AuthService:
    def __init__(self, container: Container, audit: AuditService) -> None:
        self._c = container
        self._audit = audit
        self._sessions = SessionService(container)

    @staticmethod
    def _user_lock(user_id: uuid.UUID) -> asyncio.Lock:
        """Serializa NO PROCESSO as transacoes autenticadas de um mesmo usuario: sem isto, uma rajada espera o
        `FOR UPDATE` da mesma linha segurando uma conexao cada e esgota o pool para todas as rotas."""
        lock = _USER_LOCKS.get(user_id)
        if lock is None:
            lock = _USER_LOCKS[user_id] = asyncio.Lock()
        return lock

    @staticmethod
    def _ip_key(meta: RequestMeta) -> str:
        return f"ip:{throttle_ip(meta.ip)}"

    def _pair_key(self, username: str, meta: RequestMeta) -> str:
        """Chave do bloqueio duro: (usuario, IP). Hash estavel: usuario inexistente tem o mesmo estado.
        IPv6 entra pelo prefixo /64 (ver `throttle_ip`)."""
        return f"p:{self._c.user_tag(username)}|{throttle_ip(meta.ip)}"

    async def _released_elsewhere(self, username: str, pair_key: str) -> bool:
        """True se outro processo (CLI, outro worker) desbloqueou o usuario apos este o bloquear.

        Compara `users.lock_epoch`, no maximo a cada `unlock_recheck_seconds` e so quando ha bloqueio."""
        tag = self._c.user_tag(username)
        if self._c.user_throttle.retry_after(pair_key) <= 0 or not self._c.lock_epochs.due(tag):
            return False
        async with self._c.sessions.begin() as db:
            epoch = await db.scalar(select(UserRow.lock_epoch).where(UserRow.username == username))
        if epoch is None or not self._c.lock_epochs.changed(tag, epoch):
            return False
        self._c.user_throttle.clear_prefix(f"p:{tag}|")
        self._c.user_pressure.clear(tag)
        return True

    async def _throttle_guard(self, username: str, pair_key: str, meta: RequestMeta) -> int:
        await self._released_elsewhere(username, pair_key)
        return max(
            self._c.ip_throttle.retry_after(self._ip_key(meta)), self._c.user_throttle.retry_after(pair_key)
        )

    def _register_failure(self, pair_key: str, meta: RequestMeta) -> None:
        self._c.ip_throttle.register_failure(self._ip_key(meta))
        self._c.user_throttle.register_failure(pair_key)

    def _register_success(self, pair_key: str, meta: RequestMeta) -> None:
        self._c.ip_throttle.register_success(self._ip_key(meta))
        self._c.user_throttle.register_success(pair_key)

    def _started(self, token: str, session: SessionRow, now: datetime) -> Started:
        return Started(
            token=token,
            session_id=session.id,
            expires_at=session.expires_at,
            max_age_seconds=max(1, int((session.absolute_expires_at - now).total_seconds())),
        )

    def _note_user_failure(self, user: UserRow, now: datetime) -> None:
        user.failed_attempts += 1
        if delay := lockout_seconds(user.failed_attempts, LOCKOUT_THRESHOLD):
            user.locked_until = now + timedelta(seconds=delay)

    async def _rate_limited(
        self, meta: RequestMeta, pair_key: str, retry_after: int, db: AsyncSession | None = None
    ) -> TooManyRequests:
        if self._c.audit_sampler.should_emit(f"rl:{pair_key}"):
            await self._audit.record(
                actor="anonymous",
                action=AuditAction.LOGIN_RATE_LIMITED,
                target="auth",
                result=AuditResult.DENIED,
                meta=meta,
                details={"retry_after_seconds": retry_after, "tag": pair_key.split("|", 1)[0]},
                session=db,
            )
        return TooManyRequests(
            f"Muitas tentativas. Tente de novo em {max(1, -(-retry_after // 60))} min.",
            retry_after=retry_after,
        )

    async def _verify(self, stored_hash: str | None, password: str) -> bool:
        """Argon2 fora de qualquer transacao e sem lock, atras do semaforo global de hashes concorrentes."""
        async with self._c.hash_gate.slot():
            return await self._c.passwords.verify(stored_hash, password)

    async def _rehash(self, password: str) -> str:
        async with self._c.hash_gate.slot():
            return await self._c.passwords.hash(password)

    async def _login_failed(self, tag: str, user: UserRow | None, meta: RequestMeta) -> None:
        failures = self._c.user_pressure.register_failure(tag)
        # texto livre digitado nunca vai para a auditoria: so o nome de quem existe ou o rotulo HMAC
        shown = {"attempted_user": user.username} if user is not None else {"attempted_tag": tag}
        target = target_of(user.id) if user else "user:unknown"
        async with self._c.sessions.begin() as db:
            await self._audit.record(
                actor="anonymous",
                action=AuditAction.LOGIN_FAILED,
                target=target,
                result=AuditResult.FAILURE,
                meta=meta,
                details=shown,
                session=db,
            )
            if failures >= self._c.settings.login_alert_failures and self._c.audit_sampler.should_emit(
                f"targeted:{tag}"
            ):
                log.warning("login_failures_against_one_user", failures=failures, tag=tag)
                await self._audit.record(
                    actor="anonymous",
                    action=AuditAction.LOGIN_TARGETED,
                    target=target,
                    result=AuditResult.DENIED,
                    meta=meta,
                    details={**shown, "failures_in_window": failures},
                    session=db,
                )

    async def _read_user(self, name: str) -> UserRow | None:
        async with self._c.sessions.begin() as db:
            user = await db.scalar(select(UserRow).where(UserRow.username == name))
            if user is not None:
                db.expunge(user)
        if user is not None and self._c.lock_epochs.changed(self._c.user_tag(name), user.lock_epoch):
            self._c.user_throttle.clear_prefix(f"p:{self._c.user_tag(name)}|")
        return user

    async def login(self, *, username: str, password: str, meta: RequestMeta) -> LoginOutcome:
        name = normalize_username(username)
        tag = self._c.user_tag(name)
        pair = self._pair_key(name, meta)
        if retry := await self._throttle_guard(name, pair, meta):
            raise await self._rate_limited(meta, pair, retry)
        # Reserva: a tentativa conta ANTES de verificar (sem await entre checar e registrar), entao uma
        # rajada concorrente nao passa inteira; a senha correta devolve a reserva.
        self._register_failure(pair, meta)
        try:
            if delay := self._c.user_pressure.delay_seconds(tag):
                await asyncio.sleep(delay)
            user = await self._read_user(name) if is_valid_username(name) else None
            usable = user if user is not None and not user.disabled else None
            ok = await self._verify(usable.password_hash if usable else None, password)
            upgraded = (
                await self._rehash(password)
                if ok and usable is not None and self._c.passwords.needs_rehash(usable.password_hash)
                else None
            )
        except TooManyRequests:
            self._c.ip_throttle.refund(self._ip_key(meta))
            self._c.user_throttle.refund(pair)
            raise
        if not ok or usable is None:
            await self._login_failed(tag, user, meta)
            raise Unauthorized(GENERIC_LOGIN_ERROR)
        self._c.user_throttle.register_success(pair)
        self._c.ip_throttle.refund(self._ip_key(meta))
        now = self._c.clock()
        async with self._c.sessions.begin() as db:
            row = await db.get(UserRow, usable.id)
            if row is None or row.disabled or row.password_hash != usable.password_hash:
                raise Unauthorized(GENERIC_LOGIN_ERROR)
            if upgraded:
                row.password_hash = upgraded
            token, session = self._sessions.new_row(row.id, now, meta, verified=False)
            db.add(session)
            await db.flush()
            stage = "totp" if row.totp_enabled else "enrollment"
            await self._audit.record(
                actor=row.username,
                action=AuditAction.LOGIN_PASSWORD_OK,
                target=target_of(row.id),
                result=AuditResult.SUCCESS,
                meta=meta,
                details={"next": stage},
                session=db,
            )
            return LoginOutcome(self._started(token, session, now), stage)

    async def _consume_totp(self, db: AsyncSession, user: UserRow, code: str, now: datetime) -> bool:
        if not user.totp_secret:
            return False
        try:
            secret = self._c.keyring.decrypt(user.totp_secret, aad=totp_aad(user.id))
        except DecryptionError:
            return False
        counter = totp.match_counter(secret, code, now, last_counter=user.totp_last_counter)
        if counter is None:
            return False
        result = await db.execute(
            update(UserRow)
            .where(
                UserRow.id == user.id,
                or_(UserRow.totp_last_counter.is_(None), UserRow.totp_last_counter < counter),
            )
            .values(totp_last_counter=counter)
        )
        if _rowcount(result) != 1:
            return False
        user.totp_last_counter = counter
        return True

    async def _consume_recovery(self, db: AsyncSession, user: UserRow, code: str, now: datetime) -> bool:
        digests = [hash_recovery_code(self._c.recovery_secret, code)]
        if (legacy := self._c.recovery_legacy_secret) is not None:
            digests.append(hash_recovery_code(legacy, code))
        result = await db.execute(
            update(RecoveryCodeRow)
            .where(
                RecoveryCodeRow.user_id == user.id,
                RecoveryCodeRow.code_hash.in_(digests),
                RecoveryCodeRow.used_at.is_(None),
            )
            .values(used_at=now)
        )
        return _rowcount(result) == 1

    async def _load_pending(self, db: AsyncSession, principal: Principal) -> tuple[UserRow, SessionRow]:
        user = await db.get(UserRow, principal.user_id, with_for_update=True)
        session = await db.get(SessionRow, principal.session_id, with_for_update=True)
        if user is None or session is None or session.revoked_at is not None:
            raise Unauthorized(GENERIC_LOGIN_ERROR)
        return user, session

    async def _second_factor_failed(
        self,
        db: AsyncSession,
        user: UserRow,
        session: SessionRow | None,
        meta: RequestMeta,
        now: datetime,
        action: AuditAction,
        details: dict[str, Any],
        message: str = "Codigo invalido ou ja usado. Use o proximo codigo que aparecer no app.",
    ) -> Unauthorized:
        self._register_failure(self._pair_key(user.username, meta), meta)
        self._note_user_failure(user, now)
        if session is not None:
            session.pending_failures += 1
            if session.pending_failures >= MAX_PENDING_FAILURES:
                await self._sessions.revoke(db, session.id, "too_many_failures")
        await self._audit.record(
            actor=user.username,
            action=action,
            target=target_of(user.id),
            result=AuditResult.FAILURE,
            meta=meta,
            details=details,
            session=db,
        )
        return Unauthorized(message)

    async def _activate(
        self, db: AsyncSession, user: UserRow, pending: SessionRow, meta: RequestMeta, now: datetime
    ) -> Started:
        """Rotaciona: a sessao pendente morre e nasce outra, com token e id novos."""
        token, row = self._sessions.new_row(user.id, now, meta, verified=True)
        db.add(row)
        await self._sessions.revoke(db, pending.id, "rotated")
        user.failed_attempts = 0
        user.locked_until = None
        await self._sessions.purge_old(db)
        await db.flush()
        self._register_success(self._pair_key(user.username, meta), meta)
        return self._started(token, row, now)

    async def verify_second_factor(
        self, principal: Principal, *, code: str | None, recovery_code: str | None, meta: RequestMeta
    ) -> ActivatedSession:
        user_key = self._pair_key(principal.username, meta)
        if retry := await self._throttle_guard(principal.username, user_key, meta):
            raise await self._rate_limited(meta, user_key, retry)
        if not principal.totp_enabled:
            raise BadRequest("Configure o 2FA antes de continuar.")
        now = self._c.clock()
        failure: Unauthorized | TooManyRequests | None = None
        result: ActivatedSession | None = None
        async with self._user_lock(principal.user_id), self._c.sessions.begin() as db:
            user, pending = await self._load_pending(db, principal)
            if user.locked_until and user.locked_until > now:
                failure = await self._rate_limited(
                    meta, user_key, int((user.locked_until - now).total_seconds()) + 1, db
                )
            else:
                used_recovery = recovery_code is not None
                ok = (
                    await self._consume_recovery(db, user, recovery_code, now)
                    if recovery_code is not None
                    else await self._consume_totp(db, user, code or "", now)
                )
                if not ok:
                    failure = await self._second_factor_failed(
                        db,
                        user,
                        pending,
                        meta,
                        now,
                        AuditAction.SECOND_FACTOR_FAILED,
                        {"method": "recovery" if used_recovery else "totp"},
                    )
                else:
                    started = await self._activate(db, user, pending, meta, now)
                    await self._audit.record(
                        actor=user.username,
                        action=AuditAction.LOGIN_SUCCEEDED,
                        target=target_of(user.id),
                        result=AuditResult.SUCCESS,
                        meta=meta,
                        details={"method": "recovery" if used_recovery else "totp"},
                        session=db,
                    )
                    if used_recovery:
                        await self._audit.record(
                            actor=user.username,
                            action=AuditAction.RECOVERY_CODE_USED,
                            target=target_of(user.id),
                            result=AuditResult.SUCCESS,
                            meta=meta,
                            session=db,
                        )
                    result = ActivatedSession(started, user, [], used_recovery)
        if failure is not None:
            raise failure
        assert result is not None  # noqa: S101
        return result

    async def begin_enrollment(
        self, principal: Principal, *, activation_token: str, meta: RequestMeta
    ) -> EnrollmentInfo:
        """Sem o token de ativacao (fora de banda), quem so sabe a senha nao registra TOTP."""
        user_key = self._pair_key(principal.username, meta)
        if retry := await self._throttle_guard(principal.username, user_key, meta):
            raise await self._rate_limited(meta, user_key, retry)
        now = self._c.clock()
        failure: Unauthorized | TooManyRequests | None = None
        info: EnrollmentInfo | None = None
        async with self._user_lock(principal.user_id), self._c.sessions.begin() as db:
            user, pending = await self._load_pending(db, principal)
            if user.totp_enabled:
                raise Conflict("O 2FA ja esta configurado para esta conta.")
            if user.locked_until and user.locked_until > now:
                failure = await self._rate_limited(
                    meta, user_key, int((user.locked_until - now).total_seconds()) + 1, db
                )
            elif not activation_matches(user, activation_token, now):
                failure = await self._second_factor_failed(
                    db,
                    user,
                    pending,
                    meta,
                    now,
                    AuditAction.ACTIVATION_FAILED,
                    {"method": "activation"},
                    message="Token de ativacao invalido ou expirado. Peca um novo ao administrador.",
                )
            else:
                secret = totp.new_secret()
                user.totp_secret = self._c.keyring.encrypt(secret, aad=totp_aad(user.id))
                user.totp_last_counter = None
                uri = totp.provisioning_uri(
                    secret, username=user.username, issuer=self._c.settings.totp_issuer
                )
                info = EnrollmentInfo(uri=uri, secret=secret, qr_data_uri=qr_data_uri(uri))
        if failure is not None:
            raise failure
        assert info is not None  # noqa: S101
        return info

    async def _replace_recovery_codes(self, db: AsyncSession, user: UserRow) -> list[str]:
        await db.execute(delete(RecoveryCodeRow).where(RecoveryCodeRow.user_id == user.id))
        codes = new_recovery_codes()
        db.add_all(
            RecoveryCodeRow(
                id=uuid.uuid4(), user_id=user.id, code_hash=hash_recovery_code(self._c.recovery_secret, code)
            )
            for code in codes
        )
        return codes

    async def confirm_enrollment(
        self, principal: Principal, *, code: str, meta: RequestMeta
    ) -> ActivatedSession:
        user_key = self._pair_key(principal.username, meta)
        if retry := await self._throttle_guard(principal.username, user_key, meta):
            raise await self._rate_limited(meta, user_key, retry)
        now = self._c.clock()
        failure: Unauthorized | TooManyRequests | None = None
        result: ActivatedSession | None = None
        async with self._user_lock(principal.user_id), self._c.sessions.begin() as db:
            user, pending = await self._load_pending(db, principal)
            if user.totp_enabled or not user.totp_secret:
                raise Conflict("Inicie a configuracao do 2FA antes de confirmar o codigo.")
            if user.locked_until and user.locked_until > now:
                failure = await self._rate_limited(
                    meta, user_key, int((user.locked_until - now).total_seconds()) + 1, db
                )
            elif (
                not user.activation_token_hash
                or user.activation_expires_at is None
                or user.activation_expires_at <= now
            ):
                failure = await self._second_factor_failed(
                    db,
                    user,
                    pending,
                    meta,
                    now,
                    AuditAction.ACTIVATION_FAILED,
                    {"method": "activation_expired"},
                    message="Token de ativacao expirado. Peca um novo ao administrador.",
                )
            elif not await self._consume_totp(db, user, code, now):
                failure = await self._second_factor_failed(
                    db, user, pending, meta, now, AuditAction.SECOND_FACTOR_FAILED, {"method": "enrollment"}
                )
            else:
                user.activation_token_hash = None
                user.activation_expires_at = None
                user.totp_enabled = True
                codes = await self._replace_recovery_codes(db, user)
                started = await self._activate(db, user, pending, meta, now)
                await self._audit.record(
                    actor=user.username,
                    action=AuditAction.ACTIVATION_USED,
                    target=target_of(user.id),
                    result=AuditResult.SUCCESS,
                    meta=meta,
                    session=db,
                )
                await self._audit.record(
                    actor=user.username,
                    action=AuditAction.TWO_FACTOR_ENROLLED,
                    target=target_of(user.id),
                    result=AuditResult.SUCCESS,
                    meta=meta,
                    session=db,
                )
                await self._audit.record(
                    actor=user.username,
                    action=AuditAction.LOGIN_SUCCEEDED,
                    target=target_of(user.id),
                    result=AuditResult.SUCCESS,
                    meta=meta,
                    details={"method": "enrollment"},
                    session=db,
                )
                result = ActivatedSession(started, user, codes, False)
        if failure is not None:
            raise failure
        assert result is not None  # noqa: S101
        return result

    async def reauthenticate(self, principal: Principal, *, code: str, meta: RequestMeta) -> datetime:
        user_key = self._pair_key(principal.username, meta)
        if retry := await self._throttle_guard(principal.username, user_key, meta):
            raise await self._rate_limited(meta, user_key, retry)
        now = self._c.clock()
        failure: Unauthorized | TooManyRequests | None = None
        async with self._user_lock(principal.user_id), self._c.sessions.begin() as db:
            user = await db.get(UserRow, principal.user_id, with_for_update=True)
            session = await db.get(SessionRow, principal.session_id, with_for_update=True)
            if user is None or session is None:
                raise Unauthorized()
            if user.locked_until and user.locked_until > now:
                failure = await self._rate_limited(
                    meta, user_key, int((user.locked_until - now).total_seconds()) + 1, db
                )
            elif not await self._consume_totp(db, user, code, now):
                failure = await self._second_factor_failed(
                    db, user, None, meta, now, AuditAction.REAUTH_FAILED, {"method": "totp"}
                )
            else:
                session.fresh_auth_at = now
                user.failed_attempts = 0
                await self._audit.record(
                    actor=user.username,
                    action=AuditAction.REAUTH_SUCCEEDED,
                    target=target_of(user.id),
                    result=AuditResult.SUCCESS,
                    meta=meta,
                    session=db,
                )
                self._register_success(user_key, meta)
        if failure is not None:
            raise failure
        return now

    async def regenerate_recovery_codes(self, principal: Principal, meta: RequestMeta) -> list[str]:
        async with self._user_lock(principal.user_id), self._c.sessions.begin() as db:
            user = await db.get(UserRow, principal.user_id, with_for_update=True)
            if user is None or not user.totp_enabled:
                raise Conflict("Ative o 2FA antes de gerar codigos de recuperacao.")
            codes = await self._replace_recovery_codes(db, user)
            await self._audit.record(
                actor=user.username,
                action=AuditAction.RECOVERY_CODES_REGENERATED,
                target=target_of(user.id),
                result=AuditResult.SUCCESS,
                meta=meta,
                session=db,
            )
        return codes

    async def logout(self, principal: Principal, meta: RequestMeta) -> None:
        async with self._c.sessions.begin() as db:
            await self._sessions.revoke(db, principal.session_id, "logout")
            await self._audit.record(
                actor=principal.username,
                action=AuditAction.LOGOUT,
                target=f"session:{principal.session_id}",
                result=AuditResult.SUCCESS,
                meta=meta,
                session=db,
            )

    async def change_password(
        self, principal: Principal, *, current: str, new: str, meta: RequestMeta
    ) -> None:
        """Argon2 (verificar e gerar) roda FORA de transacao e atras do HashGate; a tentativa e reservada no
        throttle antes de verificar. A transacao so rechecka o hash lido e grava."""
        user_key = self._pair_key(principal.username, meta)
        if retry := await self._throttle_guard(principal.username, user_key, meta):
            raise await self._rate_limited(meta, user_key, retry)
        self._register_failure(user_key, meta)
        new_hash = ""
        try:
            async with self._c.sessions.begin() as db:
                stored = await db.scalar(select(UserRow.password_hash).where(UserRow.id == principal.user_id))
            if stored is None:
                raise Unauthorized()
            verified = await self._verify(stored, current)
            problems = password_problems(new, username=principal.username) if verified else []
            if verified and not problems and current != new:
                new_hash = await self._rehash(new)
        except TooManyRequests:
            self._c.ip_throttle.refund(self._ip_key(meta))
            self._c.user_throttle.refund(user_key)
            raise
        now = self._c.clock()
        failure: Unauthorized | WeakPassword | Conflict | None = None
        async with self._user_lock(principal.user_id), self._c.sessions.begin() as db:
            user = await db.get(UserRow, principal.user_id, with_for_update=True)
            if user is None:
                raise Unauthorized()
            if not verified:
                self._note_user_failure(user, now)
                await self._audit.record(
                    actor=user.username,
                    action=AuditAction.PASSWORD_CHANGE_FAILED,
                    target=target_of(user.id),
                    result=AuditResult.FAILURE,
                    meta=meta,
                    details={"reason": "senha atual incorreta"},
                    session=db,
                )
                failure = Unauthorized("A senha atual nao confere.")
            elif problems:
                failure = WeakPassword(" ".join(problems))
            elif current == new:
                failure = Conflict("A nova senha deve ser diferente da atual.")
            elif user.password_hash != stored:
                failure = Conflict("A senha foi alterada em outra sessao. Tente de novo.")
            else:
                user.password_hash = new_hash
                user.password_changed_at = now
                user.failed_attempts = 0
                user.locked_until = None
                await self._sessions.revoke_user_sessions(
                    db, user.id, "password_changed", keep=principal.session_id
                )
                await self._audit.record(
                    actor=user.username,
                    action=AuditAction.PASSWORD_CHANGED,
                    target=target_of(user.id),
                    result=AuditResult.SUCCESS,
                    meta=meta,
                    session=db,
                )
        if verified:
            self._register_success(user_key, meta)
        if failure is not None:
            raise failure
