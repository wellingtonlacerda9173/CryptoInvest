"""Revisao QA 7B-1 (2a rodada) da trava de ativacao: concorrencia, limite diario frouxo no run e revogacao sem
efeitos colaterais. Banco descartavel `*_test`; nada de Live e ligado e nenhuma chamada a Binance."""

import asyncio
from typing import Any

import pytest
from sqlalchemy import URL

from engine.application.live_authorization import LIVE_PHRASE
from engine.infrastructure.persistence.models import LiveAuthorizationRevocationRow, LiveAuthorizationRow
from engine.infrastructure.persistence.session import create_engine, create_session_factory
from tests.integration.api.conftest import Harness, Session
from tests.integration.api.live_support import GOOD, seed_paper_run
from tests.integration.api.runner_support import (
    fake_link,
    module_db_url,
    module_sessions,
    rh,
    rh_admin,
    rh_reader,
    seed_run,
)
from tests.integration.api.test_live_activation import add_key, body, codes, count

__all__ = ["fake_link", "module_db_url", "module_sessions", "rh", "rh_admin", "rh_reader"]

pytestmark = pytest.mark.integration

LOOSE = "paper-live-loose-daily"


@pytest.fixture(scope="module")
def seeded_round2(module_db_url: URL) -> None:
    async def go() -> None:
        engine = create_engine(module_db_url)
        try:
            sessions = create_session_factory(engine)
            await seed_run(sessions)
            await seed_paper_run(sessions, GOOD)
            await seed_paper_run(
                sessions,
                LOOSE,
                big_loss="-300",
                risk={"max_daily_loss": "0.05", "risk_per_trade": "0.01"},
            )
        finally:
            await engine.dispose()

    asyncio.run(go())


async def test_two_simultaneous_activations_write_one_authorization_and_revoking_it_has_no_side_effects(
    rh: Harness, rh_admin: Session, seeded_round2: None
) -> None:
    await add_key(rh_admin)
    commands_before = (await rh_admin.get("/control/commands", params={"limit": 100})).json()["items"]
    status_before = (await rh_admin.get("/status")).json()

    first, second = await asyncio.gather(
        rh_admin.send("POST", "/live/activate", json=body()),
        rh_admin.send("POST", "/live/activate", json=body()),
    )

    assert sorted([first.status_code, second.status_code]) == [201, 409]
    assert await count(rh, LiveAuthorizationRow) == 1
    revoked = await rh_admin.send("POST", "/live/authorization/revoke", json={"reason": "revisao de risco"})
    assert revoked.status_code == 200, revoked.text
    assert (await rh_admin.get("/control/commands", params={"limit": 100})).json()["items"] == commands_before
    assert (await rh_admin.get("/status")).json()["mode"] == status_before["mode"]
    assert await count(rh, LiveAuthorizationRevocationRow) == 1


async def test_run_with_loosened_daily_loss_limit_cannot_unlock_live(
    rh: Harness, rh_admin: Session, seeded_round2: None
) -> None:
    await add_key(rh_admin)
    phase: dict[str, Any] = {}

    response = await rh_admin.send("POST", "/live/activate", json=body(run_id=LOOSE))
    phase["status"] = response.status_code

    assert response.status_code == 409 and "daily_loss" in codes(response), phase
    assert LIVE_PHRASE
