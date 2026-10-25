"""QA (reverificacao Fase 6): reserva atomica com papeis restritos e migracao 0014 (bancos *_test)."""

import asyncio
from decimal import Decimal

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from engine.infrastructure.config.settings import Settings
from engine.infrastructure.persistence.ai_call_repository import SqlAiCallStore
from engine.infrastructure.persistence.session import create_engine, create_session_factory
from tests.integration.disposable_db import RoleSetup, disposable_database, restricted_roles
from tests.integration.test_ai_calls import MONTH, alembic_config, call
from tests.integration.test_ai_calls import db_settings as db_settings

pytestmark = pytest.mark.integration


async def _reserve_race(roles: RoleSetup) -> None:
    engines = [create_engine(roles.app_url) for _ in range(3)]
    try:
        stores = [SqlAiCallStore(create_session_factory(e)) for e in engines]
        reserve = call("reserve", "0.07", key=None)
        ids = await asyncio.gather(
            *(stores[i % 3].reserve(reserve, budget=Decimal("1"), month_start=MONTH) for i in range(60))
        )
        granted = [i for i in ids if i is not None]
        assert len(granted) == 14  # floor(1 / 0.07)
        assert (await stores[0].usage(MONTH)).spent_usd == Decimal("0.98")
        await stores[1].settle(call("ok", "0.01"), granted[0])
        assert (await stores[2].usage(MONTH)).spent_usd == Decimal("0.92")
        for statement in ("UPDATE ai_calls SET cost_usd = 0", "DELETE FROM ai_calls"):
            with pytest.raises(DBAPIError, match=r"permission denied|must be owner"):
                async with engines[0].begin() as conn:
                    await conn.execute(text(statement))
        # orfa: sem liquidacao a reserva segue contando; nova reserva que estoura o teto e recusada
        assert (
            await stores[0].reserve(call("reserve", "0.10", key=None), budget=Decimal("1"), month_start=MONTH)
            is None
        )
    finally:
        for e in engines:
            await e.dispose()


def test_reserve_under_restricted_robot_role_and_three_engines_never_exceeds_the_budget(
    db_settings: Settings,
) -> None:
    with disposable_database(db_settings, "airesv") as url:
        command.upgrade(alembic_config(url), "head")
        with restricted_roles(db_settings, url) as roles:
            asyncio.run(_reserve_race(roles))


def test_migration_0014_up_down_up_and_alembic_check(db_settings: Settings) -> None:
    async def columns(url: object) -> set[str]:
        eng = create_engine(url)  # type: ignore[arg-type]
        try:
            async with eng.connect() as conn:
                rows = await conn.execute(
                    text("SELECT column_name FROM information_schema.columns WHERE table_name = 'ai_calls'")
                )
                return {r[0] for r in rows}
        finally:
            await eng.dispose()

    with disposable_database(db_settings, "mig14") as url:
        cfg = alembic_config(url)
        command.upgrade(cfg, "head")
        assert "reservation_id" in asyncio.run(columns(url))
        command.check(cfg)
        command.downgrade(cfg, "0013")
        assert "reservation_id" not in asyncio.run(columns(url))
        command.upgrade(cfg, "head")
        command.check(cfg)
        assert "reservation_id" in asyncio.run(columns(url))
