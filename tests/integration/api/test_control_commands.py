"""Controle do robo: comandos assinados, fila duravel, entrega, confirmacao, travas e Live indisponivel."""

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from api.services.commands import CommandService
from engine.runner.command_channel import ReplayGuard, verify_envelope
from engine.runner.paper_control import CommandName, CommandQueue
from tests.integration.api.conftest import Harness, Session, fake_key
from tests.integration.api.runner_support import (
    COMMAND_KEY,
    RUN_ID,
    build_harness,
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


def verify(link: FakeLink, index: int = -1) -> Any:
    return verify_envelope(link.pushed[index], COMMAND_KEY.encode(), link.signed_now, ReplayGuard())


async def audit_actions(admin: Session, prefix: str = "control.") -> list[dict[str, Any]]:
    return (await admin.get("/audit", params={"action": prefix, "limit": 200})).json()["items"]  # type: ignore[no-any-return]


# --------------------------------------------------------------------------- acesso e CSRF


async def test_reader_and_anonymous_cannot_command_the_robot(
    rh: Harness, rh_reader: Session, fake_link: FakeLink
) -> None:
    alive(rh, fake_link)
    for path, body in (
        ("/control/pause", {"reason": "teste"}),
        ("/control/stop", {}),
        ("/control/emergency-stop", {}),
        ("/control/power", {"on": False}),
    ):
        assert (await rh_reader.send("POST", path, json=body)).status_code == 403, path
    assert fake_link.pushed == []
    anonymous = rh.client()
    assert (await anonymous.post("/control/emergency-stop", json={})).status_code == 401
    await anonymous.aclose()


async def test_commands_need_the_csrf_token_and_a_trusted_origin(
    rh: Harness, rh_admin: Session, fake_link: FakeLink
) -> None:
    alive(rh, fake_link)
    no_csrf = await rh_admin.client.post("/control/pause", json={"reason": "sem csrf"})
    assert no_csrf.status_code == 403
    evil = await rh_admin.client.post(
        "/control/pause",
        json={"reason": "outra origem"},
        headers={**rh_admin.headers, "Origin": "https://evil.example"},
    )
    assert evil.status_code == 403 and fake_link.pushed == []


# --------------------------------------------------------------------------- entrega assinada


async def test_pause_is_signed_delivered_and_audited_with_the_operator(
    rh: Harness, rh_admin: Session, fake_link: FakeLink
) -> None:
    alive(rh, fake_link)
    fake_link.signed_now = rh.clock()
    response = await rh_admin.send(
        "POST", "/control/pause", json={"reason": "vou mexer na corretora", "operator_name": "Ana"}
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["state"] == "delivered" and body["channel"] == "redis" and body["name"] == "pause"
    assert body["operator"] == f"Ana ({rh_admin.username})" and body["message"] == "Comando enviado ao robô."
    assert body["expires_at"] is not None

    outcome = verify(fake_link)
    assert outcome.discarded is None and outcome.command_id == body["id"]
    assert outcome.command is not None and outcome.command.name is CommandName.PAUSE
    assert outcome.command.reason == "vou mexer na corretora" and outcome.command.operator == body["operator"]

    [entry] = [e for e in await audit_actions(rh_admin) if e["action"] == "control.command.submitted"]
    assert entry["actor"] == rh_admin.username and entry["details"]["command"] == "pause"
    assert entry["details"]["reason"] == "vou mexer na corretora" and entry["result"] == "success"
    assert "sig" not in json.dumps(entry)
    assert (await rh_admin.get(f"/control/commands/{body['id']}")).json()["state"] == "delivered"


async def test_unauthenticated_or_tampered_envelopes_are_not_what_the_api_sends(
    rh: Harness, rh_admin: Session, fake_link: FakeLink
) -> None:
    alive(rh, fake_link)
    fake_link.signed_now = rh.clock()
    await rh_admin.send("POST", "/control/pause", json={"reason": "um motivo"})
    envelope = json.loads(fake_link.pushed[-1])
    assert set(envelope) == {"payload", "sig"} and len(envelope["sig"]) == 64
    envelope["payload"]["name"] = "emergency-stop"
    wrong = verify_envelope(json.dumps(envelope), COMMAND_KEY.encode(), fake_link.signed_now, ReplayGuard())
    assert wrong.discarded == "bad_signature"


async def test_every_control_action_maps_to_the_runner_command(
    rh: Harness, rh_admin: Session, fake_link: FakeLink
) -> None:
    alive(rh, fake_link, halted=False, paused=True, state="PAUSED")
    fake_link.signed_now = rh.clock()
    sent = [
        ("/control/resume", {"reason": "voltou", "operator_name": "Ana"}, "resume"),
        ("/control/stop", {}, "stop"),
        ("/control/emergency-stop", {}, "emergency-stop"),
    ]
    for path, body, name in sent:
        response = await rh_admin.send("POST", path, json=body)
        assert response.status_code == 202 and response.json()["name"] == name, (path, response.text)
    names = [verify(fake_link, i).command.name.value for i in range(len(fake_link.pushed))]
    assert names == ["resume", "stop", "emergency-stop"]


# --------------------------------------------------------------------------- parar tudo


async def test_emergency_stop_blocks_without_selling_by_default_and_never_expires(
    rh: Harness, rh_admin: Session, fake_link: FakeLink
) -> None:
    alive(rh, fake_link)
    fake_link.signed_now = rh.clock()
    body = (await rh_admin.send("POST", "/control/emergency-stop", json={})).json()
    assert (
        body["liquidate"] is False and body["expires_at"] is None and body["reason"].startswith("Parar tudo")
    )
    assert verify(fake_link).command.liquidate is False  # type: ignore[union-attr]


async def test_selling_everything_needs_name_and_reason_and_lasts_thirty_minutes(
    rh: Harness, rh_admin: Session, fake_link: FakeLink
) -> None:
    alive(rh, fake_link)
    fake_link.signed_now = rh.clock()
    for bad in (
        {"liquidate": True},
        {"liquidate": True, "operator_name": "Ana"},
        {"liquidate": True, "reason": "incidente grave"},
    ):
        assert (await rh_admin.send("POST", "/control/emergency-stop", json=bad)).status_code == 422
    assert fake_link.pushed == []
    ok = await rh_admin.send(
        "POST",
        "/control/emergency-stop",
        json={"liquidate": True, "operator_name": "Ana", "reason": "incidente grave"},
    )
    body = ok.json()
    assert ok.status_code == 202 and body["liquidate"] is True
    assert verify(fake_link).command.liquidate is True  # type: ignore[union-attr]
    from datetime import datetime

    created = datetime.fromisoformat(body["created_at"])
    assert datetime.fromisoformat(body["expires_at"]) - created == timedelta(minutes=30)


# --------------------------------------------------------------------------- sem conexao: fila duravel


async def test_command_waits_in_a_durable_queue_when_the_robot_is_offline_and_is_delivered_later(
    rh: Harness, rh_admin: Session, fake_link: FakeLink
) -> None:
    body = (await rh_admin.send("POST", "/control/pause", json={"reason": "robo fora"})).json()
    assert body["state"] == "pending_delivery" and body["channel"] is None and "fila" in body["message"]
    assert (
        "Pendente de entrega" in body["state_text"]
        and "não está enviando sinal de vida" in body["state_text"]
    )
    assert fake_link.pushed == []
    assert (await rh_admin.get("/status")).json()["pending_commands"] >= 1

    alive(rh, fake_link)
    fake_link.signed_now = rh.clock()
    assert await CommandService(rh.container).dispatch_pending() >= 1
    delivered = (await rh_admin.get(f"/control/commands/{body['id']}")).json()
    assert delivered["state"] == "delivered" and delivered["channel"] == "redis"
    assert verify(fake_link).command_id == body["id"]


async def test_the_queue_survives_an_api_restart(
    rh: Harness, rh_admin: Session, fake_link: FakeLink, module_sessions: Any
) -> None:
    body = (await rh_admin.send("POST", "/control/stop", json={"reason": "antes do reinicio"})).json()
    other = build_harness(module_sessions, fake_link, fake_link.source)
    alive(other, fake_link)
    fake_link.signed_now = other.clock()
    await CommandService(other.container).dispatch_pending()
    assert verify(fake_link).command_id == body["id"]


async def test_redis_down_keeps_the_command_pending_with_the_reason(
    rh: Harness, rh_admin: Session, fake_link: FakeLink
) -> None:
    alive(rh, fake_link)
    fake_link.reachable = False
    body = (await rh_admin.send("POST", "/control/pause", json={"reason": "redis caiu"})).json()
    assert body["state"] == "pending_delivery" and "Redis" in body["state_text"]
    fake_link.reachable = True
    fake_link.signed_now = rh.clock()
    await CommandService(rh.container).dispatch_pending()
    assert (await rh_admin.get(f"/control/commands/{body['id']}")).json()["state"] == "delivered"


async def test_a_refused_push_keeps_the_command_pending(
    rh: Harness, rh_admin: Session, fake_link: FakeLink
) -> None:
    alive(rh, fake_link)
    fake_link.accepting = False
    body = (await rh_admin.send("POST", "/control/pause", json={"reason": "push recusado"})).json()
    assert body["state"] == "pending_delivery" and "não aceitou" in body["state_text"]


async def test_ordinary_commands_expire_but_the_emergency_stop_waits_as_long_as_needed(
    rh: Harness, rh_admin: Session, fake_link: FakeLink
) -> None:
    pause = (await rh_admin.send("POST", "/control/pause", json={"reason": "vai vencer"})).json()
    emergency = (await rh_admin.send("POST", "/control/emergency-stop", json={})).json()
    sell = (
        await rh_admin.send(
            "POST",
            "/control/emergency-stop",
            json={"liquidate": True, "operator_name": "Ana", "reason": "vender tudo"},
        )
    ).json()
    rh.clock.advance(minutes=11)
    service = CommandService(rh.container)
    await service.reconcile()
    assert (await rh_admin.get(f"/control/commands/{pause['id']}")).json()["state"] == "expired"
    assert (await rh_admin.get(f"/control/commands/{emergency['id']}")).json()["state"] == "pending_delivery"
    assert (await rh_admin.get(f"/control/commands/{sell['id']}")).json()["state"] == "pending_delivery"
    rh.clock.advance(minutes=20)
    await service.reconcile()
    assert (await rh_admin.get(f"/control/commands/{sell['id']}")).json()["state"] == "expired"
    still = (await rh_admin.get(f"/control/commands/{emergency['id']}")).json()
    assert still["state"] == "pending_delivery" and still["expires_at"] is None
    alive(rh, fake_link)
    fake_link.signed_now = rh.clock()
    await service.dispatch_pending()
    assert (await rh_admin.get(f"/control/commands/{emergency['id']}")).json()["state"] == "delivered"


async def test_a_command_made_before_a_runner_restart_is_expired_not_delivered(
    rh: Harness, rh_admin: Session, fake_link: FakeLink
) -> None:
    body = (await rh_admin.send("POST", "/control/pause", json={"reason": "antes do reinicio"})).json()
    emergency = (await rh_admin.send("POST", "/control/emergency-stop", json={})).json()
    rh.clock.advance(minutes=1)
    alive(rh, fake_link, started_at=rh.clock().isoformat())
    fake_link.signed_now = rh.clock()
    await CommandService(rh.container).dispatch_pending()
    old = (await rh_admin.get(f"/control/commands/{body['id']}")).json()
    assert old["state"] == "expired" and "reiniciou" in old["detail"]
    assert (await rh_admin.get(f"/control/commands/{emergency['id']}")).json()["state"] == "delivered"


async def test_file_channel_is_the_fallback_when_redis_does_not_accept(
    rh: Harness, module_sessions: Any, fake_link: FakeLink, tmp_path: Path
) -> None:
    other = build_harness(module_sessions, fake_link, fake_link.source, runner_data_dir=tmp_path)
    (tmp_path / RUN_ID).mkdir()
    admin = await other.login(await other.create_user("admin"))
    alive(other, fake_link)
    fake_link.accepting = False
    body = (await admin.send("POST", "/control/pause", json={"reason": "pelo arquivo"})).json()
    assert body["state"] == "delivered" and body["channel"] == "file"
    [(_, queued)] = CommandQueue(tmp_path / RUN_ID / "commands").pending()
    assert queued.name is CommandName.PAUSE and queued.reason == "pelo arquivo"
    await admin.aclose()


# --------------------------------------------------------------------------- confirmacao do runner


async def delivered_pause(
    rh: Harness, admin: Session, link: FakeLink, name: str = "/control/pause"
) -> dict[str, Any]:
    alive(rh, link)
    link.signed_now = rh.clock()
    body = {"reason": "teste de confirmacao"} if name != "/control/emergency-stop" else {}
    return (await admin.send("POST", name, json=body)).json()  # type: ignore[no-any-return]


@pytest.mark.parametrize(
    ("ack", "state"),
    [("applied", "applied"), ("failed", "failed"), ("discarded", "discarded")],
)
async def test_runner_acknowledgements_move_the_state(
    rh: Harness, rh_admin: Session, fake_link: FakeLink, ack: str, state: str
) -> None:
    body = await delivered_pause(rh, rh_admin, fake_link)
    fake_link.acks[body["id"]] = {"result": "received"}
    await CommandService(rh.container).reconcile()
    received = (await rh_admin.get(f"/control/commands/{body['id']}")).json()
    assert received["state"] == "delivered" and received["state_text"] == "Recebido pelo robô, aplicando."
    fake_link.acks[body["id"]] = {"result": ack, "detail": "bad_signature" if ack == "discarded" else "x"}
    await CommandService(rh.container).reconcile()
    final = (await rh_admin.get(f"/control/commands/{body['id']}")).json()
    assert final["state"] == state and final["resolved_at"]
    resolved = [e for e in await audit_actions(rh_admin) if e["action"] == "control.command.resolved"]
    assert any(e["details"]["state"] == state for e in resolved)


async def test_without_confirmation_a_command_expires_but_the_emergency_stop_is_resent(
    rh: Harness, rh_admin: Session, fake_link: FakeLink
) -> None:
    pause = await delivered_pause(rh, rh_admin, fake_link)
    emergency = await delivered_pause(rh, rh_admin, fake_link, "/control/emergency-stop")
    rh.clock.advance(seconds=95)
    await CommandService(rh.container).reconcile()
    assert (await rh_admin.get(f"/control/commands/{pause['id']}")).json()["state"] == "expired"
    again = (await rh_admin.get(f"/control/commands/{emergency['id']}")).json()
    assert again["state"] == "pending_delivery" and "reenviando" in again["state_text"]
    alive(rh, fake_link)
    fake_link.signed_now = rh.clock()
    await CommandService(rh.container).dispatch_pending()
    assert (await rh_admin.get(f"/control/commands/{emergency['id']}")).json()["state"] == "delivered"
    assert len(fake_link.pushed) == 3


async def test_command_history_is_readable_by_the_reader(
    rh: Harness, rh_admin: Session, rh_reader: Session, fake_link: FakeLink
) -> None:
    await delivered_pause(rh, rh_admin, fake_link)
    page = (await rh_reader.get("/control/commands", params={"limit": 2})).json()
    assert len(page["items"]) == 2 and page["next_cursor"]
    nxt = (
        await rh_reader.get("/control/commands", params={"limit": 50, "cursor": page["next_cursor"]})
    ).json()
    assert {c["id"] for c in page["items"]}.isdisjoint(c["id"] for c in nxt["items"])
    assert (await rh_reader.get("/control/commands/" + "0" * 32)).status_code == 404


# --------------------------------------------------------------------------- reativar, retomar, ligar


async def test_resume_is_refused_while_halted_and_reactivate_needs_fresh_two_factor(
    rh: Harness, rh_admin: Session, fake_link: FakeLink
) -> None:
    alive(rh, fake_link, state="HALTED", halted=True, halt_reason="drawdown 0.05")
    refused = await rh_admin.send(
        "POST", "/control/resume", json={"reason": "voltei", "operator_name": "Ana"}
    )
    assert refused.status_code == 409 and "Reativar" in refused.json()["detail"]
    ok = await rh_admin.send(
        "POST", "/control/reactivate", json={"reason": "revisei o ocorrido", "operator_name": "Ana"}
    )
    assert ok.status_code == 202 and ok.json()["name"] == "reactivate"
    rh.clock.advance(minutes=6)
    alive(rh, fake_link, state="HALTED", halted=True)
    stale = await rh_admin.send(
        "POST", "/control/reactivate", json={"reason": "revisei de novo", "operator_name": "Ana"}
    )
    assert stale.status_code == 403 and stale.json()["type"].endswith("fresh-auth-required")
    for missing in ({"reason": "so motivo"}, {"operator_name": "Ana"}):
        assert (await rh_admin.send("POST", "/control/reactivate", json=missing)).status_code in {403, 422}


async def test_reactivating_a_robot_that_is_not_halted_is_a_conflict(
    rh: Harness, rh_admin: Session, fake_link: FakeLink
) -> None:
    alive(rh, fake_link)
    response = await rh_admin.send(
        "POST", "/control/reactivate", json={"reason": "sem necessidade", "operator_name": "Ana"}
    )
    assert response.status_code == 409


async def test_power_off_on_and_the_launcher_guard(
    rh: Harness, rh_admin: Session, fake_link: FakeLink
) -> None:
    alive(rh, fake_link)
    fake_link.signed_now = rh.clock()
    off = (await rh_admin.send("POST", "/control/power", json={"on": False})).json()
    assert off["state"] == "commanded" and off["command"]["name"] == "stop"
    assert (await rh_admin.send("POST", "/control/power", json={"on": True})).status_code == 409
    alive(rh, fake_link, state="PAUSED", paused=True)
    on = (await rh_admin.send("POST", "/control/power", json={"on": True})).json()
    assert on["state"] == "commanded" and on["command"]["name"] == "resume"

    fake_link.latest.clear()
    assert (await rh_admin.send("POST", "/control/power", json={"on": False})).status_code == 409
    waiting = (await rh_admin.send("POST", "/control/power", json={"on": True})).json()
    assert waiting["state"] == "waiting_runner" and "Inicie" in waiting["message"]

    class Launcher:
        calls = 0

        async def launch(self) -> None:
            Launcher.calls += 1

    rh.container.launcher = Launcher()
    first = (await rh_admin.send("POST", "/control/power", json={"on": True})).json()
    second = (await rh_admin.send("POST", "/control/power", json={"on": True})).json()
    assert first["state"] == second["state"] == "launched" and Launcher.calls == 1


# --------------------------------------------------------------------------- Live


async def test_live_activation_is_refused_while_criteria_are_unmet_and_never_changes_anything(
    rh: Harness, rh_admin: Session, rh_reader: Session, fake_link: FakeLink
) -> None:
    alive(rh, fake_link)
    before = (await rh_admin.get("/status")).json()
    commands_before = (await rh_admin.get("/control/commands", params={"limit": 100})).json()["items"]
    for body in (
        {},
        {"phrase": "OPERAR COM DINHEIRO REAL"},
        {"phrase": "OPERAR COM DINHEIRO REAL", "code": rh.code_for(rh_admin.username)},
    ):
        response = await rh_admin.send("POST", "/live/activate", json=body)
        assert response.status_code == 409 and response.json()["detail"].startswith("Live nao autorizado"), (
            body
        )
        assert {e["code"] for e in response.json()["errors"]} >= {"no_test", "operating_days", "phrase"} - (
            {"phrase"} if body.get("phrase") else set()
        )
    assert (await rh_reader.send("POST", "/live/activate", json={})).status_code == 403
    after = (await rh_admin.get("/status")).json()
    assert after["mode"] == before["mode"] == "paper" and after["live"]["live_available"] is False
    assert (await rh_admin.get("/control/commands", params={"limit": 100})).json()["items"] == commands_before
    refused = [e for e in await audit_actions(rh_admin, "live.") if e["action"] == "live.activation.refused"]
    assert (
        len(refused) == 3
        and refused[0]["result"] == "denied"
        and any(u["criterion"] == "operating_days" for u in refused[0]["details"]["unmet"])
    )


async def test_live_activation_needs_a_fresh_two_factor_before_even_answering(
    rh: Harness, rh_admin: Session
) -> None:
    rh.clock.advance(minutes=6)
    stale = await rh_admin.send("POST", "/live/activate", json={})
    assert stale.status_code == 403 and stale.json()["type"].endswith("fresh-auth-required")


# --------------------------------------------------------------------------- chaves da Binance


async def test_removing_the_binance_key_is_refused_while_the_robot_is_on(
    rh: Harness, rh_admin: Session, fake_link: FakeLink
) -> None:
    key, secret = fake_key()
    created = await rh_admin.send(
        "POST", "/credentials", json={"label": "Principal", "api_key": key, "api_secret": secret}
    )
    credential_id = created.json()["id"]
    alive(rh, fake_link)
    refused = await rh_admin.send("DELETE", f"/credentials/{credential_id}")
    assert refused.status_code == 409 and "Pare o robô" in refused.json()["detail"]
    assert any(c["id"] == credential_id for c in (await rh_admin.get("/credentials")).json())
    fake_link.reachable = False
    unknown = await rh_admin.send("DELETE", f"/credentials/{credential_id}")
    assert unknown.status_code == 409 and "confirmar que o robô está parado" in unknown.json()["detail"]
    fake_link.reachable = True
    fake_link.latest.clear()
    assert (await rh_admin.send("DELETE", f"/credentials/{credential_id}")).status_code == 204
    refusals = [
        e for e in await audit_actions(rh_admin, "credential.") if e["action"] == "credential.delete_refused"
    ]
    assert len(refusals) == 2
