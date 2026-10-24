"""Versoes da configuracao do robo (append-only). O runner le a ultima no start; a API grava cada mudanca."""

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from engine.application.trading_config import ConfigDocument
from engine.infrastructure.persistence.models import AuditLogRow, ConfigVersionRow
from engine.infrastructure.persistence.serialization import ConflictError

GENESIS = "0" * 64
# serializa as gravacoes da cadeia (a 1a versao concorrente tambem: NULL nao e protegido por UNIQUE)
_APPEND_LOCK_KEY = 0x43494346  # "CICF"
CONFLICT_MESSAGE = "a configuração mudou enquanto você editava; recarregue e tente de novo"


class ConfigConflictError(ConflictError):
    """Outra versao foi gravada ao mesmo tempo; releia a configuracao e tente de novo."""


@dataclass(frozen=True, slots=True)
class ConfigVersion:
    id: int
    parent_id: int | None
    created_at: datetime
    actor: str
    reason: str
    sections: tuple[str, ...]
    activation: str
    risk_increase: bool
    document: ConfigDocument
    diff: list[dict[str, Any]]
    config_hash: str
    chain_hash: str


def chain_of(previous_chain: str, config_hash: str, actor: str, reason: str) -> str:
    return hashlib.sha256(f"{previous_chain}|{config_hash}|{actor}|{reason}".encode()).hexdigest()


def to_version(row: ConfigVersionRow) -> ConfigVersion:
    return ConfigVersion(
        id=row.id,
        parent_id=row.parent_id,
        created_at=row.created_at,
        actor=row.actor,
        reason=row.reason,
        sections=tuple(row.sections),
        activation=row.activation,
        risk_increase=row.risk_increase,
        document=ConfigDocument.model_validate(row.document),
        diff=list(row.diff),
        config_hash=row.config_hash,
        chain_hash=row.chain_hash,
    )


class ConfigRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def latest(self) -> ConfigVersion | None:
        stmt = select(ConfigVersionRow).order_by(ConfigVersionRow.id.desc()).limit(1)
        row = await self._session.scalar(stmt)
        return None if row is None else to_version(row)

    async def get(self, version_id: int) -> ConfigVersion | None:
        row = await self._session.get(ConfigVersionRow, version_id)
        return None if row is None else to_version(row)

    async def history(self, *, limit: int = 50, before_id: int | None = None) -> list[ConfigVersion]:
        stmt = select(ConfigVersionRow).order_by(ConfigVersionRow.id.desc()).limit(limit)
        if before_id is not None:
            stmt = stmt.where(ConfigVersionRow.id < before_id)
        return [to_version(row) for row in await self._session.scalars(stmt)]

    async def append(
        self,
        document: ConfigDocument,
        *,
        actor: str,
        reason: str,
        activation: str,
        risk_increase: bool,
        sections: list[str],
        diff: list[dict[str, Any]],
        expected_parent: int | None,
    ) -> ConfigVersion:
        """Grava a nova versao se a anterior ainda for `expected_parent` (controle otimista, sem corrida)."""
        await self._session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _APPEND_LOCK_KEY})
        parent = await self.latest()
        if (parent.id if parent else None) != expected_parent:
            raise ConfigConflictError(CONFLICT_MESSAGE)
        config_hash = document.digest()
        row = ConfigVersionRow(
            parent_id=expected_parent,
            actor=actor[:100],
            reason=reason,
            sections=sections,
            activation=activation,
            risk_increase=risk_increase,
            document=document.dump(),
            diff=diff,
            config_hash=config_hash,
            chain_hash=chain_of(parent.chain_hash if parent else GENESIS, config_hash, actor[:100], reason),
        )
        self._session.add(row)
        try:
            await self._session.flush()
        except IntegrityError as error:
            raise ConfigConflictError(CONFLICT_MESSAGE) from error
        await self._session.refresh(row)
        return to_version(row)


async def load_latest(sessions: async_sessionmaker[AsyncSession]) -> ConfigVersion | None:
    async with sessions() as session:
        return await ConfigRepository(session).latest()


async def load_run_document(sessions: async_sessionmaker[AsyncSession], run_id: str) -> ConfigDocument | None:
    """Documento efetivo gravado no último `paper_start` do run (ausente em runs iniciados só pela CLI)."""
    stmt = (
        select(AuditLogRow.payload)
        .where(AuditLogRow.entity == run_id, AuditLogRow.action == "paper_start")
        .order_by(AuditLogRow.id.desc())
        .limit(1)
    )
    async with sessions() as session:
        payload = await session.scalar(stmt)
    source = ((payload or {}).get("config") or {}).get("config_source") or {}
    document = source.get("document")
    return None if document is None else ConfigDocument.model_validate(document)
