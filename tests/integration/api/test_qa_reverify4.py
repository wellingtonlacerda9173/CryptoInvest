"""QA 2a rodada: A2 de ponta a ponta, concorrencia de PUTs, canal de arquivo e honestidade do estado."""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from api.services.commands import CommandService
from tests.integration.api.conftest import Harness, Session
from tests.integration.api.runner_support import (
    build_harness,
    fake_link,
    module_db_url,
    module_sessions,
    rh,
    rh_admin,
    rh_reader,
)
from tests.integration.api.test_control_hardening import alive, put, robot_as_saved
from tests.redis_fakes import FakeLink

pytestmark = pytest.mark.integration
__all__ = ["fake_link", "module_db_url", "module_sessions", "rh", "rh_admin", "rh_reader"]


async def test_two_concurrent_puts_with_the_same_expected_version_save_exactly_one(
    rh: Harness, rh_admin: Session, fake_link: FakeLink
) -> None:
    await robot_as_saved(rh, rh_admin, fake_link)
    current = (await rh_admin.get("/settings")).json()["version"]
    expected = 0 if current is None else current["id"]

    async def send(spread: str) -> Any:
        return await rh_admin.send(
            "PUT",
            "/settings/risk",
            json={
                "value": {"max_entry_deviation": spread},
                "reason": "corrida",
                "expected_version": expected,
            },
        )

    first, second = await asyncio.gather(send("0.0031"), send("0.0032"))

    assert sorted([first.status_code, second.status_code]) == [200, 409], (first.text, second.text)
    history = (await rh_admin.get("/settings/history", params={"limit": 50})).json()["items"]
    roots = [v for v in history if v["parent_id"] is None]
    assert len(roots) == 1
    assert len({v["parent_id"] for v in history}) == len(history), "ramificacao: dois filhos do mesmo pai"


async def test_a_mixed_version_reports_what_the_robot_did_with_its_reduction(
    rh: Harness, rh_admin: Session, fake_link: FakeLink
) -> None:
    await robot_as_saved(rh, rh_admin, fake_link)
    response = await put(
        rh_admin, "risk", {"max_drawdown": "0.04", "max_daily_loss": "0.02"}, acknowledge_risk_increase=True
    )
    assert response.status_code == 200, response.text
    body = response.json()
    command_id, version_id = body["command"]["id"], body["version"]["id"]
    fake_link.acks[command_id] = {
        "result": "partially_applied",
        "detail": "aplicado: drawdown; recusado: perda",
    }
    await CommandService(rh.container).reconcile()

    history = (await rh_admin.get("/settings/history", params={"limit": 50})).json()["items"]
    shown = next(v for v in history if v["id"] == version_id)

    assert shown["application"] == "partially_applied", shown["application_text"]


async def test_a_stale_reduction_command_that_never_got_an_ack_expires_and_the_version_says_so(
    rh: Harness, rh_admin: Session, fake_link: FakeLink
) -> None:
    await robot_as_saved(rh, rh_admin, fake_link)
    body = (await put(rh_admin, "risk", {"max_entry_deviation": "0.0029"})).json()
    version_id, command_id = body["version"]["id"], body["command"]["id"]
    assert body["version"]["application"] == "pending"

    rh.clock.advance(seconds=100)
    await CommandService(rh.container).reconcile()

    command = (await rh_admin.get(f"/control/commands/{command_id}")).json()
    history = (await rh_admin.get("/settings/history", params={"limit": 50})).json()["items"]
    shown = next(v for v in history if v["id"] == version_id)
    assert command["state"] == "expired"
    assert shown["application"] == "expired" and "próximo início" in shown["application_text"]


async def test_the_pushed_reduction_only_carries_the_fields_that_lower_risk_and_never_an_increase(
    rh: Harness, rh_admin: Session, fake_link: FakeLink
) -> None:
    await robot_as_saved(rh, rh_admin, fake_link)
    saved = (await rh_admin.get("/settings")).json()["document"]["risk"]
    lower = f"{float(saved['max_drawdown']) / 2:.4f}"
    higher = f"{float(saved['risk_per_trade']) * 1.1:.5f}"
    response = await put(
        rh_admin, "risk", {"risk_per_trade": higher, "max_drawdown": lower}, acknowledge_risk_increase=True
    )
    assert response.status_code == 200, response.text
    pushed = json.loads(fake_link.pushed[-1])["payload"]["params"]["risk"]
    assert "risk_per_trade" not in pushed and pushed["max_drawdown"] == lower


async def test_a_file_channel_command_to_a_dead_run_eventually_resolves(
    rh: Harness, module_sessions: Any, fake_link: FakeLink, tmp_path: Path
) -> None:
    other = build_harness(module_sessions, fake_link, fake_link.source, runner_data_dir=tmp_path)
    (tmp_path / "paper-it-1").mkdir()
    admin = await other.login(await other.create_user("admin"))
    alive(other, fake_link)
    fake_link.accepting = False
    body = (await admin.send("POST", "/control/pause", json={"reason": "run morreu depois"})).json()
    assert body["channel"] == "file" and body["state"] == "delivered"

    other.clock.advance(seconds=3 * 24 * 3600)
    fake_link.latest.clear()
    await CommandService(other.container).reconcile()

    # a sessao HTTP vence junto com o relogio simulado: le o estado direto do servico
    final = await CommandService(other.container).get(body["id"])
    assert final.state != "delivered", "um pedido de 10 min de validade segue 'entregue' apos 3 dias"
    assert final.state == "expired"
    await admin.aclose()
