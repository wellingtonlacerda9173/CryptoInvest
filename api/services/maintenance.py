from dataclasses import dataclass

from sqlalchemy import select

from api.audit import SYSTEM_META, AuditAction, AuditResult, AuditService
from api.container import Container
from api.persistence.models import ExchangeCredentialRow, UserRow
from api.security.crypto import DecryptionError
from api.services.credentials import key_aad, secret_aad
from api.services.users import totp_aad


@dataclass(frozen=True, slots=True)
class ReencryptReport:
    totp_secrets: int
    credentials: int
    failed: tuple[str, ...] = ()


async def reencrypt_all(container: Container, audit: AuditService, *, actor: str) -> ReencryptReport:
    """Rotacao da chave mestra: recifra tudo com a versao ativa (idempotente).

    As linhas ficam travadas (FOR UPDATE, ordem fixa): rotacao concorrente de credencial nao e revertida."""
    keyring = container.keyring
    totp_count = credential_count = 0
    failed: list[str] = []
    async with container.sessions.begin() as db:
        for user in (
            await db.scalars(
                select(UserRow).where(UserRow.totp_secret.is_not(None)).order_by(UserRow.id).with_for_update()
            )
        ).all():
            if user.totp_secret and keyring.needs_rotation(user.totp_secret):
                try:
                    plain = keyring.decrypt(user.totp_secret, aad=totp_aad(user.id))
                except DecryptionError:
                    failed.append(f"user:{user.id}")
                    continue
                user.totp_secret = keyring.encrypt(plain, aad=totp_aad(user.id))
                totp_count += 1
        for row in (
            await db.scalars(
                select(ExchangeCredentialRow).order_by(ExchangeCredentialRow.id).with_for_update()
            )
        ).all():
            if keyring.needs_rotation(row.api_key_enc) or keyring.needs_rotation(row.api_secret_enc):
                try:
                    key = keyring.decrypt(row.api_key_enc, aad=key_aad(row.id))
                    secret = keyring.decrypt(row.api_secret_enc, aad=secret_aad(row.id))
                except DecryptionError:
                    failed.append(f"credential:{row.id}")
                    continue
                row.api_key_enc = keyring.encrypt(key, aad=key_aad(row.id))
                row.api_secret_enc = keyring.encrypt(secret, aad=secret_aad(row.id))
                credential_count += 1
        await audit.record(
            actor=actor,
            action=AuditAction.SECRETS_REENCRYPTED,
            target="system:master_key",
            result=AuditResult.SUCCESS,
            meta=SYSTEM_META,
            details={
                "active_version": keyring.active_version,
                "totp_secrets": totp_count,
                "credentials": credential_count,
                "failed": failed,
            },
            session=db,
        )
    return ReencryptReport(totp_count, credential_count, tuple(failed))
