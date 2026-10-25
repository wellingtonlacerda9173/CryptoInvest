"""Configuração versionada no início do runner: a última versão do banco manda; a CLI é o plano B."""

from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from engine.application.strategies import Strategy
from engine.application.trading_config import effective_document
from engine.application.trading_config.builders import build_strategies_from_config
from engine.infrastructure.persistence.config_repository import load_latest, load_run_document
from engine.infrastructure.persistence.recovery import load_recovery_state
from engine.runner.paper_config import PaperRunConfig

log = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class VersionedStart:
    config: PaperRunConfig
    strategies: list[Strategy] | None
    source: dict[str, Any]


async def apply_versioned_config(
    sessions: async_sessionmaker[AsyncSession], base: PaperRunConfig, run_id: str, *, enabled: bool = True
) -> VersionedStart:
    """Lê `config_versions` e monta o run. Retomada com posição aberta não aceita aumento de risco (ver
    `effective_document`); sem versão gravada, vale a configuração da CLI."""
    if not enabled:
        return VersionedStart(base, None, {"origin": "cli", "reason": "configuração versionada desligada"})
    version = await load_latest(sessions)
    if version is None:
        return VersionedStart(base, None, {"origin": "cli", "reason": "sem versão gravada"})
    recovery = await load_recovery_state(sessions, run_id)
    open_positions = recovery.run is not None and bool(recovery.open_positions)
    running = await load_run_document(sessions, run_id) if open_positions else None
    if open_positions and running is None:
        log.warning("versioned_config_ignored", run_id=run_id, reason="posição aberta de run sem documento")
        return VersionedStart(
            base,
            None,
            {
                "origin": "cli",
                "ignored_version": version.id,
                "reason": "posição aberta sem documento anterior",
            },
        )
    document = effective_document(version.document, running, open_positions=open_positions)
    strategies = document.strategies
    history = max(base.history_window, 2 * strategies.ema.trend_period + 50 if strategies.ema_enabled else 0)
    config = base.model_copy(
        update={
            "symbols": document.pairs.symbols,
            "timeframe": document.pairs.timeframe,
            "strategies": strategies.names,
            "params": {},
            "risk": document.risk,
            "history_window": history,
            "document_hash": document.identity_digest(),
            "ai": document.ai,
            "ai_min_confidence": strategies.min_confidence,
        }
    )
    source = {
        "origin": "versioned",
        "version_id": version.id,
        "version_hash": version.config_hash,
        "effective_hash": document.digest(),
        "restricted_by_open_positions": document.digest() != version.document.digest(),
        "document": document.dump(),
    }
    log.info("versioned_config_loaded", version=version.id, effective_hash=document.digest()[:12])
    return VersionedStart(config, build_strategies_from_config(strategies), source)
