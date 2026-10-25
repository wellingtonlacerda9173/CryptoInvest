import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic_core import to_jsonable_python
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from engine.infrastructure.config.logging import scrub_text
from engine.infrastructure.persistence.models import AuditLogRow

_SENSITIVE_KEY = re.compile(
    r"password|passwd|secret|token|key|code|signature|authorization|totp|cookie", re.I
)
_MAX_TEXT = 300
_MAX_DEPTH = 4


class AuditAction(StrEnum):
    LOGIN_PASSWORD_OK = "auth.login.password_ok"
    LOGIN_FAILED = "auth.login.failed"
    LOGIN_SUCCEEDED = "auth.login.succeeded"
    LOGIN_RATE_LIMITED = "auth.login.rate_limited"
    SECOND_FACTOR_FAILED = "auth.2fa.failed"
    TWO_FACTOR_ENROLLED = "auth.2fa.enrolled"
    RECOVERY_CODE_USED = "auth.2fa.recovery_code_used"
    RECOVERY_CODES_REGENERATED = "auth.2fa.recovery_codes_regenerated"
    REAUTH_SUCCEEDED = "auth.reauth.succeeded"
    REAUTH_FAILED = "auth.reauth.failed"
    LOGOUT = "auth.logout"
    PASSWORD_CHANGED = "auth.password.changed"
    PASSWORD_CHANGE_FAILED = "auth.password.change_failed"
    SESSION_REVOKED = "session.revoked"
    ACCESS_DENIED = "authz.denied"
    USER_CREATED = "user.created"
    USER_UPDATED = "user.updated"
    USER_TWO_FACTOR_RESET = "user.two_factor_reset"
    CREDENTIAL_CREATED = "credential.created"
    CREDENTIAL_ROTATED = "credential.rotated"
    CREDENTIAL_DELETED = "credential.deleted"
    CREDENTIAL_VERIFIED = "credential.verified"
    CREDENTIAL_REJECTED = "credential.rejected"
    SECRETS_REENCRYPTED = "system.secrets_reencrypted"
    CONTROL_COMMAND = "control.command.submitted"
    CONTROL_COMMAND_RESOLVED = "control.command.resolved"
    LIVE_ACTIVATION_REFUSED = "live.activation.refused"
    LIVE_AUTHORIZATION_GRANTED = "live.authorization.granted"
    LIVE_AUTHORIZATION_REVOKED = "live.authorization.revoked"
    LIVE_AUTHORIZATION_REVOKE_REFUSED = "live.authorization.revoke_refused"
    CONFIG_UPDATED = "config.updated"
    CONFIG_REFUSED = "config.refused"
    BACKTEST_CREATED = "backtest.created"
    BACKTEST_CANCELLED = "backtest.cancelled"
    ONBOARDING_PAPER_STARTED = "onboarding.paper_started"
    CREDENTIAL_DELETE_REFUSED = "credential.delete_refused"
    USER_UNLOCKED = "user.unlocked"
    ACTIVATION_ISSUED = "user.activation_issued"
    ACTIVATION_USED = "auth.activation.used"
    ACTIVATION_FAILED = "auth.activation.failed"
    LOGIN_TARGETED = "auth.login.targeted"
    AI_LESSON_REVIEWED = "ai.lesson.reviewed"
    AI_LESSON_RUN_REQUESTED = "ai.lesson.run_requested"
    AI_LESSON_RUN_REFUSED = "ai.lesson.run_refused"


class AuditResult(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    DENIED = "denied"


@dataclass(frozen=True, slots=True)
class RequestMeta:
    ip: str | None
    user_agent: str | None
    request_id: str | None


SYSTEM_META = RequestMeta(ip=None, user_agent="cli", request_id=None)


def sanitize(value: Any, depth: int = 0) -> Any:
    """Remove valores de chaves sensiveis e mascara segredos em texto: o audit_log nunca guarda segredo."""
    if depth > _MAX_DEPTH:
        return "..."
    if isinstance(value, str):
        return scrub_text(value)[:_MAX_TEXT]
    if isinstance(value, dict):
        return {
            str(k)[:60]: "***"
            if _SENSITIVE_KEY.search(str(k)) and not str(k).endswith("last4")
            else sanitize(v, depth + 1)
            for k, v in list(value.items())[:30]
        }
    if isinstance(value, list | tuple | set | frozenset):
        return [sanitize(item, depth + 1) for item in list(value)[:30]]
    return to_jsonable_python(value, fallback=str)


class AuditService:
    """Grava no `audit_log` (append-only). Com `session`, entra na mesma transacao da acao auditada."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def record(
        self,
        *,
        actor: str,
        action: AuditAction,
        target: str,
        result: AuditResult,
        meta: RequestMeta,
        details: dict[str, Any] | None = None,
        session: AsyncSession | None = None,
    ) -> None:
        row = AuditLogRow(
            actor=actor[:100],
            action=action.value,
            entity=target[:100],
            payload={
                "result": result.value,
                "ip": meta.ip,
                "user_agent": (meta.user_agent or "")[:200] or None,
                "request_id": meta.request_id,
                "details": sanitize(details or {}),
            },
        )
        if session is not None:
            session.add(row)
            return
        async with self._sessions.begin() as own:
            own.add(row)
