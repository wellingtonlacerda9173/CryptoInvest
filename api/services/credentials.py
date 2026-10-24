import uuid
from typing import Any

from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from api.audit import AuditAction, AuditResult, AuditService, RequestMeta
from api.binance_keys import KeyPermissions, KeyVerificationError
from api.container import Container
from api.errors import ApiError, Conflict, NotFound, UpstreamUnavailable
from api.persistence.models import ExchangeCredentialRow
from api.schemas import CredentialOut

WITHDRAW_MESSAGE = (
    "Esta chave pode sacar dinheiro. Crie outra chave na Binance sem a permissao de saque "
    "(apenas leitura e negociacao spot) e tente de novo."
)
NO_READ_MESSAGE = "Esta chave nao tem permissao de leitura. Habilite 'Ler' na Binance e tente de novo."
IP_WARNING = (
    "Recomendado: restrinja esta chave aos enderecos IP do servidor na Binance. "
    "Assim, mesmo vazada, ela nao funciona de outro lugar."
)


class CredentialRejected(ApiError):
    status, slug, title = 422, "credential-rejected", "Chave recusada"


def key_aad(credential_id: uuid.UUID) -> str:
    return f"exchange_credentials:{credential_id}:api_key"


def secret_aad(credential_id: uuid.UUID) -> str:
    return f"exchange_credentials:{credential_id}:api_secret"


def warnings_for(permissions: KeyPermissions) -> list[str]:
    found: list[str] = []
    if permissions.ip_restricted is not True:
        found.append(IP_WARNING)
    if not permissions.can_trade:
        found.append(
            "Esta chave nao permite negociar. Basta para o Paper; o Live exige permissao de negociacao spot."
        )
    if permissions.can_transfer:
        found.append("Esta chave permite transferencias internas. Desabilite se nao for necessario.")
    if permissions.margin_enabled or permissions.futures_enabled:
        found.append("Esta chave habilita margem/futuros. O robo opera apenas spot; desabilite o excedente.")
    return found


def to_out(row: ExchangeCredentialRow) -> CredentialOut:
    permissions = KeyPermissions.model_validate(row.permissions)
    return CredentialOut(
        id=row.id,
        exchange="binance",
        label=row.label,
        api_key_last4=row.api_key_last4,
        permissions=permissions,
        usable=permissions.can_read and not permissions.can_withdraw,
        warnings=warnings_for(permissions),
        created_at=row.created_at,
        updated_at=row.updated_at,
        last_verified_at=row.last_verified_at,
        last_used_at=row.last_used_at,
    )


class CredentialService:
    """Chaves da corretora: gravacao write-only, cifradas em repouso e verificadas antes de gravar."""

    def __init__(self, container: Container, audit: AuditService) -> None:
        self._c = container
        self._audit = audit

    async def _verify(
        self, api_key: SecretStr, api_secret: SecretStr, *, actor: str, meta: RequestMeta, target: str
    ) -> KeyPermissions:
        try:
            permissions = await self._c.verifier.verify(
                api_key.get_secret_value(), api_secret.get_secret_value()
            )
        except KeyVerificationError as exc:
            if exc.upstream:
                raise UpstreamUnavailable(str(exc)) from None
            await self._audit.record(
                actor=actor,
                action=AuditAction.CREDENTIAL_REJECTED,
                target=target,
                result=AuditResult.FAILURE,
                meta=meta,
                details={"reason": "verificacao recusada pela corretora"},
            )
            raise CredentialRejected(str(exc)) from None
        reason = (
            WITHDRAW_MESSAGE
            if permissions.can_withdraw
            else None
            if permissions.can_read
            else NO_READ_MESSAGE
        )
        if reason:
            await self._audit.record(
                actor=actor,
                action=AuditAction.CREDENTIAL_REJECTED,
                target=target,
                result=AuditResult.DENIED,
                meta=meta,
                details={"can_withdraw": permissions.can_withdraw, "can_read": permissions.can_read},
            )
            raise CredentialRejected(reason)
        return permissions

    async def create(
        self,
        *,
        label: str,
        api_key: SecretStr,
        api_secret: SecretStr,
        actor: str,
        actor_id: uuid.UUID,
        meta: RequestMeta,
    ) -> ExchangeCredentialRow:
        permissions = await self._verify(
            api_key, api_secret, actor=actor, meta=meta, target=f"credential:{label}"
        )
        now = self._c.clock()
        credential_id = uuid.uuid4()
        row = ExchangeCredentialRow(
            id=credential_id,
            exchange="binance",
            label=label,
            api_key_enc=self._c.keyring.encrypt(api_key.get_secret_value(), aad=key_aad(credential_id)),
            api_secret_enc=self._c.keyring.encrypt(
                api_secret.get_secret_value(), aad=secret_aad(credential_id)
            ),
            api_key_last4=api_key.get_secret_value()[-4:],
            permissions=permissions.model_dump(mode="json"),
            created_by=actor_id,
            created_at=now,
            updated_at=now,
            last_verified_at=now,
        )
        try:
            async with self._c.sessions.begin() as db:
                db.add(row)
                await db.flush()
                await self._audit.record(
                    actor=actor,
                    action=AuditAction.CREDENTIAL_CREATED,
                    target=f"credential:{credential_id}",
                    result=AuditResult.SUCCESS,
                    meta=meta,
                    details={"label": label, "key_last4": row.api_key_last4, "permissions": row.permissions},
                    session=db,
                )
        except IntegrityError:
            raise Conflict(
                "Ja existe uma chave com esse nome. Escolha outro nome ou troque a chave existente."
            ) from None
        return row

    async def list(self) -> list[ExchangeCredentialRow]:
        async with self._c.sessions.begin() as db:
            return list(
                (
                    await db.scalars(select(ExchangeCredentialRow).order_by(ExchangeCredentialRow.created_at))
                ).all()
            )

    async def get(self, credential_id: uuid.UUID) -> ExchangeCredentialRow:
        async with self._c.sessions.begin() as db:
            row = await db.get(ExchangeCredentialRow, credential_id)
        if row is None:
            raise NotFound("Chave nao encontrada.")
        return row

    async def rotate(
        self,
        *,
        credential_id: uuid.UUID,
        api_key: SecretStr,
        api_secret: SecretStr,
        actor: str,
        meta: RequestMeta,
    ) -> ExchangeCredentialRow:
        target = f"credential:{credential_id}"
        await self.get(credential_id)
        permissions = await self._verify(api_key, api_secret, actor=actor, meta=meta, target=target)
        now = self._c.clock()
        async with self._c.sessions.begin() as db:
            row = await db.get(ExchangeCredentialRow, credential_id, with_for_update=True)
            if row is None:
                raise NotFound("Chave nao encontrada.")
            row.api_key_enc = self._c.keyring.encrypt(api_key.get_secret_value(), aad=key_aad(credential_id))
            row.api_secret_enc = self._c.keyring.encrypt(
                api_secret.get_secret_value(), aad=secret_aad(credential_id)
            )
            row.api_key_last4 = api_key.get_secret_value()[-4:]
            row.permissions = permissions.model_dump(mode="json")
            row.updated_at = row.last_verified_at = now
            row.last_used_at = None
            await self._audit.record(
                actor=actor,
                action=AuditAction.CREDENTIAL_ROTATED,
                target=target,
                result=AuditResult.SUCCESS,
                meta=meta,
                details={"label": row.label, "key_last4": row.api_key_last4, "permissions": row.permissions},
                session=db,
            )
            return row

    async def delete(self, *, credential_id: uuid.UUID, actor: str, meta: RequestMeta) -> None:
        async with self._c.sessions.begin() as db:
            row = await db.get(ExchangeCredentialRow, credential_id, with_for_update=True)
            if row is None:
                raise NotFound("Chave nao encontrada.")
            label = row.label
            await db.delete(row)
            await self._audit.record(
                actor=actor,
                action=AuditAction.CREDENTIAL_DELETED,
                target=f"credential:{credential_id}",
                result=AuditResult.SUCCESS,
                meta=meta,
                details={"label": label},
                session=db,
            )

    async def verify_stored(
        self, *, credential_id: uuid.UUID, actor: str, meta: RequestMeta
    ) -> ExchangeCredentialRow:
        key, secret = await self.reveal(credential_id, mark_used=False)
        try:
            permissions = await self._c.verifier.verify(key.get_secret_value(), secret.get_secret_value())
        except KeyVerificationError as exc:
            await self._audit.record(
                actor=actor,
                action=AuditAction.CREDENTIAL_REJECTED,
                target=f"credential:{credential_id}",
                result=AuditResult.FAILURE,
                meta=meta,
                details={"reason": str(exc)[:200], "upstream": exc.upstream, "stage": "verify_stored"},
            )
            if exc.upstream:
                raise UpstreamUnavailable(str(exc)) from None
            raise CredentialRejected(str(exc)) from None
        now = self._c.clock()
        async with self._c.sessions.begin() as db:
            row = await db.get(ExchangeCredentialRow, credential_id, with_for_update=True)
            if row is None:
                raise NotFound("Chave nao encontrada.")
            row.permissions = permissions.model_dump(mode="json")
            row.last_verified_at = now
            details: dict[str, Any] = {"permissions": row.permissions, "usable": not permissions.can_withdraw}
            await self._audit.record(
                actor=actor,
                action=AuditAction.CREDENTIAL_VERIFIED,
                target=f"credential:{credential_id}",
                result=AuditResult.DENIED if permissions.can_withdraw else AuditResult.SUCCESS,
                meta=meta,
                details=details,
                session=db,
            )
            return row

    async def reveal(
        self, credential_id: uuid.UUID, *, mark_used: bool = True
    ) -> tuple[SecretStr, SecretStr]:
        """Uso interno (runner/engine): decifra para chamar a corretora. Nunca exposto por endpoint."""
        async with self._c.sessions.begin() as db:
            row = await db.get(ExchangeCredentialRow, credential_id, with_for_update=mark_used)
            if row is None:
                raise NotFound("Chave nao encontrada.")
            key = self._c.keyring.decrypt(row.api_key_enc, aad=key_aad(credential_id))
            secret = self._c.keyring.decrypt(row.api_secret_enc, aad=secret_aad(credential_id))
            if mark_used:
                row.last_used_at = self._c.clock()
        return SecretStr(key), SecretStr(secret)
