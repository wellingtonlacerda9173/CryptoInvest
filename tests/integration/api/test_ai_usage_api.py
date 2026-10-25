"""Uso da IA para o painel: leitor e admin leem; sem sessao nao le; mostra gasto do mes e circuito."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from engine.application.ai.ledger import AiCallRecord
from engine.infrastructure.persistence.ai_call_repository import SqlAiCallStore
from tests.integration.api.conftest import Harness, Session
from tests.integration.api.runner_support import (
    fake_link,
    module_db_url,
    module_sessions,
    rh,
    rh_admin,
    rh_reader,
    status_payload,
)
from tests.redis_fakes import FakeLink

pytestmark = pytest.mark.integration


def record(result: str, cost: str, at: datetime) -> AiCallRecord:
    return AiCallRecord(
        called_at=at,
        model="claude-haiku-5-5",
        prompt_version="analyst-v1",
        prompt_hash="b" * 64,
        cost_usd=Decimal(cost),
        result=result,  # type: ignore[arg-type]
    )


async def test_usage_sums_the_current_month_and_reports_the_runner_circuit(
    rh: Harness, rh_reader: Session, rh_admin: Session, fake_link: FakeLink
) -> None:
    now = rh.clock()
    store = SqlAiCallStore(rh.sessions)
    await store.record(record("ok", "0.40", now))
    await store.record(record("timeout", "0.10", now))
    await store.record(record("budget", "0", now))
    await store.record(record("ok", "7", datetime(2020, 1, 1, tzinfo=UTC)))
    fake_link.latest["heartbeat"] = status_payload(
        now=now, ai={"status": "ligada", "enabled": True, "circuit_open": True, "calls_last_hour": 3}
    )

    for session in (rh_reader, rh_admin):
        body = (await session.get("/ai/usage")).json()
        assert body["spent_usd"] == pytest.approx(0.5) and body["calls"] == 3
        assert (body["failures"], body["budget_stops"]) == (1, 1)
        assert body["budget_usd"] is None or body["budget_usd"] == 20.0
        assert (body["runner_status"], body["circuit_open"], body["calls_last_hour"]) == ("ligada", True, 3)


async def test_usage_requires_a_session(rh: Harness) -> None:
    async with rh.client() as anonymous:
        assert (await anonymous.get("/ai/usage")).status_code == 401
