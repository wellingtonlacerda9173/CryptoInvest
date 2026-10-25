"""QA (reverificacao Fase 6): run com posicao aberta retoma apos mexer so na IA (banco *_test)."""

from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import update

from engine.application.trading_config import default_document
from engine.application.trading_config.document import AiConfig
from engine.infrastructure.persistence.models import RunRow
from engine.infrastructure.persistence.run_repository import RunRepository
from engine.runner.paper_versioned import apply_versioned_config
from tests.integration.api.runner_support import module_db_url as module_db_url
from tests.integration.api.runner_support import module_sessions as module_sessions
from tests.integration.test_versioned_start import base_config, save, seed_open_run

pytestmark = pytest.mark.integration


async def test_resume_open_run_after_changing_only_the_ai_section(module_sessions: Any) -> None:
    v1 = default_document()
    await save(module_sessions, v1, "v1 do run")
    first = await apply_versioned_config(module_sessions, base_config(), "paper-ai-resume")
    stored_hash = first.config.config_hash()
    await seed_open_run(module_sessions, "paper-ai-resume", v1)
    async with module_sessions.begin() as db:
        await db.execute(
            update(RunRow).where(RunRow.run_id == "paper-ai-resume").values(config_hash=stored_hash)
        )

    for ai in (
        AiConfig(enabled=True),
        AiConfig(enabled=True, monthly_budget_usd=Decimal(35), timeout_seconds=3.0),
        AiConfig(enabled=True, model="claude-sonnet-5-5", monthly_budget_usd=Decimal(1)),
        AiConfig(enabled=False, timeout_seconds=12.0),
    ):
        await save(module_sessions, v1.with_section("ai", ai.model_dump(mode="json")), "muda so a IA")
        again = await apply_versioned_config(module_sessions, base_config(), "paper-ai-resume")
        assert again.config.config_hash() == stored_hash, ai
        async with module_sessions.begin() as db:
            stored = await RunRepository(db).get("paper-ai-resume")
            assert stored is not None and stored.config_hash == again.config.config_hash()
