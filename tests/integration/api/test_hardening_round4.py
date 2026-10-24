"""4a rodada: reenvio limitado da parada de emergencia e gating de `effective_risk` pelo estado do robo."""

from datetime import timedelta

import pytest

from api.services.commands import EMERGENCY_ALERT_ATTEMPTS, MAX_SEND_IDS, CommandService
from api.services.settings_service import SettingsService
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
from tests.integration.api.test_control_commands import delivered_pause
from tests.integration.api.test_control_hardening import alive
from tests.redis_fakes import FakeLink

pytestmark = pytest.mark.integration

__all__ = ["fake_link", "module_db_url", "module_sessions", "rh", "rh_admin", "rh_reader"]


async def test_an_unanswered_emergency_stop_keeps_resending_with_one_pending_attempt_and_bounded_ids(
    rh: Harness, rh_admin: Session, fake_link: FakeLink
) -> None:
    body = await delivered_pause(rh, rh_admin, fake_link, "/control/emergency-stop")
    service = CommandService(rh.container)
    rounds = EMERGENCY_ALERT_ATTEMPTS + 6

    for _ in range(rounds):
        rh.clock.advance(seconds=200)
        await service.reconcile()
        rh.clock.advance(seconds=61)  # espera crescente (teto 60 s) entre as tentativas
        alive(rh, fake_link)
        await service.dispatch_pending()

    row = await service.get_row(body["id"])
    assert row.state == "delivered", "nunca desiste de parar tudo"
    assert len(fake_link.pending) == 1, "uma tentativa pendente por pedido (as anteriores foram retiradas)"
    assert len(fake_link.pushed) == rounds + 1
    assert len((row.result or {})["send_ids"]) == MAX_SEND_IDS
    assert int((row.result or {})["attempts"]) == rounds + 1


async def test_the_emergency_stop_states_the_alert_after_the_attempt_limit(
    rh: Harness, rh_admin: Session, fake_link: FakeLink
) -> None:
    body = await delivered_pause(rh, rh_admin, fake_link, "/control/emergency-stop")
    service = CommandService(rh.container)
    for _ in range(EMERGENCY_ALERT_ATTEMPTS):
        rh.clock.advance(seconds=200)
        await service.reconcile()
        alive(rh, fake_link)
        await service.dispatch_pending()
    rh.clock.advance(seconds=200)
    await service.reconcile()

    row = await service.get_row(body["id"])
    assert row.state == "pending_delivery"
    assert "tentativas" in str((row.result or {}).get("waiting"))


@pytest.mark.parametrize(
    ("overrides", "age", "expected"),
    [
        ({}, 0, True),
        ({"state": "STOPPED"}, 0, False),
        ({"state": "STOPPING"}, 0, False),
        ({"state": "PAUSED"}, 0, True),
        ({}, 600, False),
    ],
)
async def test_effective_risk_is_only_reported_for_a_robot_that_is_on(
    rh: Harness, fake_link: FakeLink, overrides: dict[str, str], age: int, expected: bool
) -> None:
    now = rh.clock()
    fake_link.latest["heartbeat"] = status_payload(now=now - timedelta(seconds=age), **overrides)
    fake_link.signed_now = now

    risk = await SettingsService(rh.container).effective_risk()

    assert (risk is not None) is expected


async def test_effective_risk_is_none_without_any_signal(rh: Harness, fake_link: FakeLink) -> None:
    fake_link.latest.clear()
    assert await SettingsService(rh.container).effective_risk() is None
