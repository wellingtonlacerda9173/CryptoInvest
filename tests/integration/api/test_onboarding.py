"""Primeiro acesso: estado dos passos e a acao "Iniciar no Paper" (nunca vai ao Live)."""

from typing import Any

import pytest

from api.services.commands import CommandService
from tests.integration.api.conftest import Harness, Session, fake_key
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


def alive(rh: Harness, link: FakeLink, **overrides: Any) -> None:
    link.latest["heartbeat"] = status_payload(now=rh.clock(), **overrides)


def steps(body: dict[str, Any]) -> dict[str, bool]:
    return {s["key"]: s["done"] for s in body["steps"]}


async def test_fresh_install_shows_what_is_missing(
    rh: Harness, rh_admin: Session, rh_reader: Session
) -> None:
    body = (await rh_reader.get("/onboarding")).json()
    assert steps(body) == {
        "admin": True,
        "two_factor": True,
        "binance_key": False,
        "risk": False,
        "paper": False,
    }
    assert body["completed"] is False and body["next_step"] == "risk" and body["can_start_paper"] is True
    assert body["paper_running"] is False and "Ficar parado em USDT é normal" in body["message"]
    optional = {s["key"]: s["required"] for s in body["steps"]}
    assert optional["binance_key"] is False and optional["risk"] is True
    assert "Opcional no Paper" in next(s["detail"] for s in body["steps"] if s["key"] == "binance_key")
    assert (await rh_reader.send("POST", "/onboarding/start-paper")).status_code == 403
    anonymous = rh.client()
    assert (await anonymous.get("/onboarding")).status_code == 401
    await anonymous.aclose()


async def test_a_saved_and_validated_binance_key_completes_its_step(rh: Harness, rh_admin: Session) -> None:
    key, secret = fake_key()
    created = await rh_admin.send(
        "POST", "/credentials", json={"label": "Principal", "api_key": key, "api_secret": secret}
    )
    assert created.status_code == 201
    body = (await rh_admin.get("/onboarding")).json()
    detail = next(s["detail"] for s in body["steps"] if s["key"] == "binance_key")
    assert steps(body)["binance_key"] is True and f"terminada em {key[-4:]}" in detail
    assert secret not in str(body) and key not in str(body)


async def test_start_paper_without_a_runner_saves_safe_defaults_and_says_how_to_start(
    rh: Harness, rh_admin: Session
) -> None:
    result = await rh_admin.send("POST", "/onboarding/start-paper")
    body = result.json()
    assert result.status_code == 202 and body["state"] == "waiting_runner" and body["config_version"]
    assert "Inicie" in body["message"]
    settings = (await rh_admin.get("/settings")).json()
    assert (
        settings["version"]["id"] == body["config_version"]
        and settings["version"]["actor"] == rh_admin.username
    )
    assert (
        settings["document"]["risk"]["risk_per_trade"] == "0.005"
        and settings["version"]["risk_increase"] is False
    )
    again = (await rh_admin.send("POST", "/onboarding/start-paper")).json()
    assert again["config_version"] == body["config_version"]
    assert steps((await rh_admin.get("/onboarding")).json())["risk"] is True
    audit = (await rh_admin.get("/audit", params={"action": "onboarding."})).json()["items"]
    assert len(audit) == 2 and audit[0]["details"]["state"] == "waiting_runner"


async def test_start_paper_launches_the_runner_once_when_a_launcher_is_configured(
    rh: Harness, rh_admin: Session
) -> None:
    class Launcher:
        calls = 0

        async def launch(self) -> None:
            Launcher.calls += 1

    rh.container.launcher = Launcher()
    first = (await rh_admin.send("POST", "/onboarding/start-paper")).json()
    second = (await rh_admin.send("POST", "/onboarding/start-paper")).json()
    assert first["state"] == second["state"] == "launched" and Launcher.calls == 1


async def test_start_paper_with_a_running_robot_resumes_or_reports_it(
    rh: Harness, rh_admin: Session, fake_link: FakeLink
) -> None:
    alive(rh, fake_link)
    running = (await rh_admin.send("POST", "/onboarding/start-paper")).json()
    assert running["state"] == "already_running" and running["command_id"] is None
    body = (await rh_admin.get("/onboarding")).json()
    assert body["paper_running"] is True and steps(body)["paper"] is True

    alive(rh, fake_link, state="PAUSED", paused=True)
    fake_link.signed_now = rh.clock()
    resumed = (await rh_admin.send("POST", "/onboarding/start-paper")).json()
    assert resumed["state"] == "resumed" and resumed["command_id"]
    assert (await CommandService(rh.container).get(resumed["command_id"])).name == "resume"

    alive(rh, fake_link, state="HALTED", halted=True)
    halted = await rh_admin.send("POST", "/onboarding/start-paper")
    assert halted.status_code == 409 and "Reativar" in halted.json()["detail"]
    assert (await rh_admin.get("/status")).json()["mode"] == "paper"
