"""Reinicio de run existente com config versionada nova: troca auditada, nunca de modo nem sem versao."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from engine.application.trading_config import ConfigDocument, default_document
from engine.domain.journal import ControlAction, ControlEvent, RunRecord
from engine.infrastructure.persistence.config_repository import ConfigRepository
from engine.infrastructure.persistence.models import AuditLogRow
from engine.infrastructure.persistence.run_config_change import current_hash
from engine.infrastructure.persistence.run_repository import RunRepository
from engine.infrastructure.persistence.serialization import PersistenceError
from engine.runner.paper_config import PaperRunConfig
from engine.runner.paper_journal import SqlJournal
from engine.runner.paper_versioned import apply_versioned_config
from tests.integration.api.runner_support import module_db_url as module_db_url
from tests.integration.api.runner_support import module_sessions as module_sessions

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
BASE = PaperRunConfig(symbols=("BTC/USDT",))


def risk_doc(risk: str) -> ConfigDocument:
    doc = default_document()
    return doc.with_section("risk", {**doc.dump()["risk"], "risk_per_trade": risk})


async def save(sessions: async_sessionmaker[AsyncSession], doc: ConfigDocument) -> None:
    async with sessions.begin() as db:
        repo = ConfigRepository(db)
        latest = await repo.latest()
        await repo.append(
            doc,
            actor="ana",
            reason="teste",
            activation="next_start",
            risk_increase=False,
            sections=["risk"],
            diff=[],
            expected_parent=latest.id if latest else None,
        )


async def start(
    sessions: async_sessionmaker[AsyncSession],
    run_id: str,
    config: PaperRunConfig,
    source: dict[str, Any],
    mode: str = "paper",
) -> None:
    run = RunRecord(
        run_id=run_id, mode=mode, started_at=NOW, config_hash=config.config_hash(), code_version="t"
    )  # type: ignore[arg-type]
    payload = {"config": config.model_dump(mode="json"), "config_source": source}
    event = ControlEvent(at=NOW, action=ControlAction.START, operator="system", reason="inicio do run")
    await SqlJournal(sessions, run_id).start_run(run, payload, event)


async def stored_hash(sessions: async_sessionmaker[AsyncSession], run_id: str) -> str:
    async with sessions() as db:
        run = await RunRepository(db).get(run_id)
    assert run is not None
    return run.config_hash


async def test_restart_applying_a_pending_version_records_the_change(module_sessions: Any) -> None:
    await save(module_sessions, risk_doc("0.0025"))
    first = await apply_versioned_config(module_sessions, BASE, "paper-a")
    await start(module_sessions, "paper-a", first.config, first.source)
    old_hash = await stored_hash(module_sessions, "paper-a")

    await save(module_sessions, risk_doc("0.005"))
    second = await apply_versioned_config(module_sessions, BASE, "paper-a")
    assert second.config.config_hash() != old_hash
    await start(module_sessions, "paper-a", second.config, second.source)

    assert await stored_hash(module_sessions, "paper-a") == old_hash
    async with module_sessions() as db:
        row = await RunRepository(db).get("paper-a")
    assert row is not None
    assert await current_hash(module_sessions, row) == second.config.config_hash()
    async with module_sessions() as db:
        changes = list(
            await db.scalars(
                select(AuditLogRow.payload).where(
                    AuditLogRow.entity == "paper-a", AuditLogRow.action == "paper_config_change"
                )
            )
        )
    assert [c["previous_config_hash"] for c in changes] == [old_hash]
    assert changes[0]["config_hash"] == second.config.config_hash()

    await start(module_sessions, "paper-a", second.config, second.source)
    async with module_sessions() as db:
        total = len(
            list(await db.scalars(select(AuditLogRow.id).where(AuditLogRow.action == "paper_config_change")))
        )
    assert total == 1


async def test_a_different_mode_is_still_refused(module_sessions: Any) -> None:
    await save(module_sessions, risk_doc("0.003"))
    first = await apply_versioned_config(module_sessions, BASE, "paper-m")
    await start(module_sessions, "paper-m", first.config, first.source)
    with pytest.raises(PersistenceError, match="modo/config diferentes"):
        await start(module_sessions, "paper-m", first.config, first.source, mode="live")


async def test_a_tampered_hash_without_a_matching_version_is_refused(module_sessions: Any) -> None:
    await save(module_sessions, risk_doc("0.003"))
    first = await apply_versioned_config(module_sessions, BASE, "paper-t")
    await start(module_sessions, "paper-t", first.config, first.source)

    forged = first.config.model_copy(update={"document_hash": "a" * 64})
    with pytest.raises(PersistenceError, match="modo/config diferentes"):
        await start(module_sessions, "paper-t", forged, first.source)

    cli = BASE.model_copy(update={"risk": risk_doc("0.009").risk})
    with pytest.raises(PersistenceError, match="modo/config diferentes"):
        await start(module_sessions, "paper-t", cli, {"origin": "cli", "reason": "x"})

    fake_version = {**first.source, "version_id": 999_999}
    with pytest.raises(PersistenceError, match="modo/config diferentes"):
        await start(
            module_sessions, "paper-t", first.config.model_copy(update={"capital": Decimal(1)}), fake_version
        )
