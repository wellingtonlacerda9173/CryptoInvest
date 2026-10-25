"""Troca controlada da configuracao de um run existente: so a que veio de uma versao gravada da cadeia.

A linha de `runs` e imutavel (o papel do robo so atualiza `ended_at`): o hash de origem fica nela e cada troca
aceita vira um `paper_config_change` no `audit_log` append-only; o hash vigente e o da ultima troca."""

from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from engine.application.trading_config import ConfigDocument
from engine.domain.journal import RunRecord
from engine.infrastructure.persistence.models import AuditLogRow, ConfigVersionRow

# campos de `trading_fields` que o documento versionado NAO controla: mudar um deles continua sendo outro run
CHANGE_ACTION = "paper_config_change"

_OUTSIDE_DOCUMENT = (
    "capital",
    "maker_fee",
    "slippage_bps",
    "latency_ms",
    "max_stop_blind_seconds",
    "max_book_age_seconds",
    "selector",
)


async def versioned_change_allowed(
    sessions: async_sessionmaker[AsyncSession], stored: RunRecord, config: Mapping[str, Any]
) -> bool:
    """O hash novo e aceito so se (1) vem de uma versao existente de `config_versions` com o hash certo, (2) o
    documento efetivo bate com o hash de identidade e (3) tudo que o documento nao controla (capital, taxas,
    seletor...) e igual ao do ultimo inicio do run."""
    source = config.get("config_source") or {}
    inner = config.get("config") or {}
    if source.get("origin") != "versioned" or not (document_hash := inner.get("document_hash")):
        return False
    try:
        effective = ConfigDocument.model_validate(source["document"])
    except (KeyError, ValueError):
        return False
    if effective.identity_digest() != document_hash:
        return False
    async with sessions() as session:
        version = await session.get(ConfigVersionRow, source.get("version_id"))
        previous = await session.scalar(
            select(AuditLogRow.payload)
            .where(AuditLogRow.entity == stored.run_id, AuditLogRow.action == "paper_start")
            .order_by(AuditLogRow.id.desc())
            .limit(1)
        )
    if version is None or version.config_hash != source.get("version_hash"):
        return False
    before = ((previous or {}).get("config") or {}).get("config")
    if not isinstance(before, dict):
        return False
    return all(before.get(name) == inner.get(name) for name in _OUTSIDE_DOCUMENT)


def change_audit(stored: RunRecord, run: RunRecord, config: Mapping[str, Any]) -> dict[str, Any]:
    source = config.get("config_source") or {}
    return {
        "previous_config_hash": stored.config_hash,
        "config_hash": run.config_hash,
        "version_id": source.get("version_id"),
        "effective_hash": source.get("effective_hash"),
    }


async def current_hash(sessions: async_sessionmaker[AsyncSession], stored: RunRecord) -> str:
    async with sessions() as session:
        latest = await session.scalar(
            select(AuditLogRow.payload)
            .where(AuditLogRow.entity == stored.run_id, AuditLogRow.action == CHANGE_ACTION)
            .order_by(AuditLogRow.id.desc())
            .limit(1)
        )
    return str((latest or {}).get("config_hash") or stored.config_hash)
