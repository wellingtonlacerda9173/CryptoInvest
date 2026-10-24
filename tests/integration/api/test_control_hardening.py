"""Controle endurecido: efeito real das reducoes, resultados explicitos dos comandos, reenvio da parada de
emergencia, canal de arquivo, cursores e confirmacoes assinadas (Redis real)."""

import json
from pathlib import Path
from typing import Any

import pytest

from api.services.commands import CommandService
from engine.domain.risk import RiskConfig
from engine.runner.command_channel import RedisCommandSource, sign_command
from engine.runner.paper_control import Command, CommandName, CommandQueue
from tests.integration.api.conftest import Harness, Session
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

__all__ = ["fake_link", "module_db_url", "module_sessions", "rh", "rh_admin", "rh_reader"]


def alive(rh: Harness, link: FakeLink, **overrides: Any) -> None:
    link.latest["heartbeat"] = status_payload(now=rh.clock(), **overrides)
    link.signed_now = rh.clock()


async def put(admin: Session, section: str, value: dict[str, Any], **extra: Any) -> Any:
    version = (await admin.get("/settings")).json()["version"]
    extra.setdefault("expected_version", 0 if version is None else version["id"])
    return await admin.send(
        "PUT", f"/settings/{section}", json={"value": value, "reason": "teste de efeito", **extra}
    )


async def robot_as_saved(rh: Harness, admin: Session, link: FakeLink, **risk: Any) -> None:
    """Robo ligado cujo risco em vigor e o do ultimo documento salvo (menos o que `risk` sobrescreve)."""
    saved = (await admin.get("/settings")).json()["document"]["risk"]
    alive(rh, link, risk={**RiskConfig().model_dump(mode="json"), **saved, **risk})


# --------------------------------------------------------------------------- A2: efeito real das reducoes


async def test_a_reduction_with_the_robot_offline_is_next_start_and_sends_nothing(
    rh: Harness, rh_admin: Session, fake_link: FakeLink
) -> None:
    pushed = len(fake_link.pushed)
    document = (await rh_admin.get("/settings")).json()["document"]["risk"]
    lower = str(float(document["max_spread"]) / 2)
    validated = (
        await rh_admin.send(
            "POST", "/settings/validate", json={"section": "risk", "value": {"max_spread": lower}}
        )
    ).json()
    assert validated["activation"] == "next_start" and "não está ligado" in validated["activation_text"]
    assert validated["changes"][0]["applies"] == "next_start"

    body = (await put(rh_admin, "risk", {"max_spread": lower})).json()
    assert body["activation"] == "next_start" and body["version"]["activation"] == "next_start"
    assert body["command"] is None and body["applied_now"] == [] and len(fake_link.pushed) == pushed
    assert body["version"]["application"] == "none"


async def test_a_reduction_is_measured_against_the_robot_not_against_the_last_saved_version(
    rh: Harness, rh_admin: Session, fake_link: FakeLink
) -> None:
    """v(n): sobe risk_per_trade p/ 0,006 (so no proximo inicio). v(n+1): 'reduz' p/ 0,0055 (queda vs a salva,
    ALTA vs o robo, que segue em 0,005) e reduz max_daily_loss. So a segunda vale agora."""
    await robot_as_saved(rh, rh_admin, fake_link)
    saved = await put(rh_admin, "risk", {"risk_per_trade": "0.006"}, acknowledge_risk_increase=True)
    assert saved.status_code == 200, saved.text
    await robot_as_saved(rh, rh_admin, fake_link, risk_per_trade="0.005")

    response = await put(rh_admin, "risk", {"risk_per_trade": "0.0055", "max_daily_loss": "0.008"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["applied_now"] == ["Perda diária máxima"]
    assert body["pending_restart"] == ["Risco por operação"]
    command = json.loads(fake_link.pushed[-1])["payload"]
    assert command["params"]["risk"] == {"max_daily_loss": "0.008"}, command["params"]


async def test_expected_version_is_mandatory_on_settings_updates(rh: Harness, rh_admin: Session) -> None:
    response = await rh_admin.send(
        "PUT", "/settings/risk", json={"value": {"max_spread": "0.0004"}, "reason": "sem versao"}
    )
    assert response.status_code == 422
    assert any("expected_version" in str(e["loc"]) for e in response.json()["errors"])


async def test_unknown_fields_in_settings_bodies_are_refused(rh: Harness, rh_admin: Session) -> None:
    response = await put(rh_admin, "risk", {"max_spread": "0.0004"}, valor_extra=1)
    assert response.status_code == 422


async def test_the_version_shows_what_the_robot_did_with_the_reduction(
    rh: Harness, rh_admin: Session, fake_link: FakeLink
) -> None:
    await robot_as_saved(rh, rh_admin, fake_link)
    reduced = await put(rh_admin, "risk", {"max_entry_deviation": "0.003"})
    assert reduced.status_code == 200, reduced.text
    body = reduced.json()
    version_id, command_id = body["version"]["id"], body["command"]["id"]
    assert body["version"]["application"] == "pending"

    fake_link.acks[command_id] = {"result": "partially_applied", "detail": "aplicado: x; recusado: y"}
    await CommandService(rh.container).reconcile()
    history = (await rh_admin.get("/settings/history", params={"limit": 50})).json()["items"]
    shown = next(v for v in history if v["id"] == version_id)
    assert shown["application"] == "partially_applied" and "só parte" in shown["application_text"]

    again = (await rh_admin.get("/control/commands/" + command_id)).json()
    assert again["state"] == "partially_applied" and "recusado: y" in again["state_text"]


# --------------------------------------------------------------------------- resultados dos comandos


@pytest.mark.parametrize(
    ("result", "detail", "text"),
    [
        ("refused", "o robô está desligado por drawdown", "Recusado pelo robô"),
        ("partially_applied", "aplicado: a; recusado: b", "Aplicado em parte"),
    ],
)
async def test_refused_and_partial_results_are_visible_with_the_reason(
    rh: Harness, rh_admin: Session, fake_link: FakeLink, result: str, detail: str, text: str
) -> None:
    alive(rh, fake_link)
    body = (await rh_admin.send("POST", "/control/pause", json={"reason": "teste de resultado"})).json()
    fake_link.acks[body["id"]] = {"result": result, "detail": detail}
    await CommandService(rh.container).reconcile()
    final = (await rh_admin.get(f"/control/commands/{body['id']}")).json()
    assert final["state"] == result and text in final["state_text"] and detail in final["state_text"]
    assert final["detail"] == detail


@pytest.mark.parametrize("code", ["stale", "future"])
async def test_the_emergency_stop_discarded_by_the_clock_is_resent_with_a_new_id_until_applied(
    rh: Harness, rh_admin: Session, fake_link: FakeLink, code: str
) -> None:
    alive(rh, fake_link)
    body = (await rh_admin.send("POST", "/control/emergency-stop", json={})).json()
    first = json.loads(fake_link.pushed[-1])["payload"]["id"]
    assert first == body["id"]

    fake_link.acks[first] = {"result": "discarded", "detail": code}
    await CommandService(rh.container).reconcile()
    row = (await rh_admin.get(f"/control/commands/{body['id']}")).json()
    assert row["state"] == "pending_delivery" and code in row["state_text"]

    rh.clock.advance(seconds=30)
    alive(rh, fake_link)
    await CommandService(rh.container).dispatch_pending()
    second = json.loads(fake_link.pushed[-1])["payload"]["id"]
    assert second != first and second.startswith(first)
    assert (await rh_admin.get(f"/control/commands/{body['id']}")).json()["state"] == "delivered"

    fake_link.acks[second] = {"result": "applied", "detail": ""}
    await CommandService(rh.container).reconcile()
    assert (await rh_admin.get(f"/control/commands/{body['id']}")).json()["state"] == "applied"


async def test_other_commands_discarded_for_any_reason_stay_discarded(
    rh: Harness, rh_admin: Session, fake_link: FakeLink
) -> None:
    alive(rh, fake_link)
    body = (await rh_admin.send("POST", "/control/pause", json={"reason": "teste de descarte"})).json()
    fake_link.acks[body["id"]] = {"result": "discarded", "detail": "stale"}
    await CommandService(rh.container).reconcile()
    assert (await rh_admin.get(f"/control/commands/{body['id']}")).json()["state"] == "discarded"


async def test_an_old_applied_ack_wins_over_a_newer_attempt_still_unanswered(
    rh: Harness, rh_admin: Session, fake_link: FakeLink
) -> None:
    alive(rh, fake_link)
    body = (await rh_admin.send("POST", "/control/emergency-stop", json={})).json()
    rh.clock.advance(seconds=100)
    await CommandService(rh.container).reconcile()
    alive(rh, fake_link)
    await CommandService(rh.container).dispatch_pending()
    fake_link.acks[body["id"]] = {"result": "applied", "detail": ""}
    await CommandService(rh.container).reconcile()
    assert (await rh_admin.get(f"/control/commands/{body['id']}")).json()["state"] == "applied"


# --------------------------------------------------------------------------- canal de arquivo


@pytest.mark.parametrize(
    ("archive", "state"),
    [("processed", "applied"), ("failed", "failed"), ("expired", "expired")],
)
async def test_the_file_channel_resolves_the_state_from_the_runner_folders(
    rh: Harness, module_sessions: Any, fake_link: FakeLink, tmp_path: Path, archive: str, state: str
) -> None:
    other = build_harness(module_sessions, fake_link, fake_link.source, runner_data_dir=tmp_path)
    (tmp_path / RUN_ID).mkdir()
    admin = await other.login(await other.create_user("admin"))
    alive(other, fake_link)
    fake_link.accepting = False
    body = (await admin.send("POST", "/control/pause", json={"reason": "pelo arquivo"})).json()
    assert body["channel"] == "file" and body["state"] == "delivered"

    queue = CommandQueue(tmp_path / RUN_ID / "commands")
    [(path, _)] = queue.pending()
    await CommandService(other.container).reconcile()
    assert (await admin.get(f"/control/commands/{body['id']}")).json()["state"] == "delivered"

    if archive == "processed":
        from engine.runner.paper_control import CommandResult

        queue.done(path, CommandResult.refused("nao pode"))
        state = "refused"
    elif archive == "failed":
        queue.fail(path)
    else:
        queue._archive(path, "expired")
    await CommandService(other.container).reconcile()
    final = (await admin.get(f"/control/commands/{body['id']}")).json()
    assert final["state"] == state and (state != "refused" or "nao pode" in final["state_text"])
    await admin.aclose()


# --------------------------------------------------------------------------- cursores


@pytest.mark.parametrize("url", ["/control/commands", "/backtests"])
@pytest.mark.parametrize(
    "cursor",
    ["lixo", "2026-01-01T00:00:00|x", "|", "9999-99-99", "2026-01-01T00:00:00|" + "a" * 32, "x" * 80],
)
async def test_malformed_or_naive_cursors_are_422(
    rh: Harness, rh_reader: Session, url: str, cursor: str
) -> None:
    response = await rh_reader.get(url, params={"cursor": cursor})
    assert response.status_code == 422, (cursor, response.text)
    assert response.headers["content-type"].startswith("application/problem+json")


async def test_a_cursor_given_by_the_api_keeps_working(
    rh: Harness, rh_admin: Session, fake_link: FakeLink
) -> None:
    alive(rh, fake_link)
    for n in range(3):
        await rh_admin.send("POST", "/control/pause", json={"reason": f"cursor {n}"})
    page = (await rh_admin.get("/control/commands", params={"limit": 2})).json()
    assert page["next_cursor"]
    nxt = await rh_admin.get("/control/commands", params={"limit": 2, "cursor": page["next_cursor"]})
    assert nxt.status_code == 200 and {i["id"] for i in nxt.json()["items"]}.isdisjoint(
        {i["id"] for i in page["items"]}
    )


@pytest.mark.parametrize("param", ["q", "strategy"])
async def test_nul_byte_in_search_text_is_a_validation_error(
    rh: Harness, rh_reader: Session, param: str
) -> None:
    for url in ("/trades", "/trades/export"):
        if param == "strategy" and url.endswith("export"):
            continue
        response = await rh_reader.get(url, params={param: "a\x00b"})
        assert response.status_code == 422, (url, response.status_code)


# --------------------------------------------------------------------------- Redis assinado de ponta a ponta


async def test_signed_acks_are_checked_and_forged_ones_are_dropped_and_counted(
    settings: Any,
) -> None:
    from api.runner_link import RedisRunnerLink
    from engine.runner.paper_events import RedisKeys
    from tests.integration.api.runner_support import redis_url_of

    keys = RedisKeys("cryptoinvest:test:signedacks")
    key = COMMAND_KEY.encode()
    link = RedisRunnerLink(redis_url_of(settings), keys, signing_key=key)
    if not (await link.probe()).reachable:
        pytest.skip("Redis indisponivel")
    source = RedisCommandSource.from_url(redis_url_of(settings), key, keys=keys)
    try:
        await source.ack("abcdefgh1", "applied", "ok")
        genuine = await link.ack("abcdefgh1")
        assert genuine is not None and genuine["result"] == "applied" and "sig" not in genuine

        raw = RedisRunnerLink(redis_url_of(settings), keys)
        await raw._client.set(keys.ack("forged0001"), json.dumps({"result": "applied", "detail": ""}))
        assert await link.ack("forged0001") is None
        assert link.rejections.total >= 1
        await raw.close()
    finally:
        await source.close()
        await link.close()


async def test_unsigned_state_published_to_redis_is_dropped_by_a_keyed_link(settings: Any) -> None:
    from api.runner_link import RedisRunnerLink
    from engine.runner.paper_events import RedisEventBus, RedisKeys
    from tests.integration.api.runner_support import redis_url_of

    keys = RedisKeys("cryptoinvest:test:signedstate")
    key = COMMAND_KEY.encode()
    keyed = RedisRunnerLink(redis_url_of(settings), keys, signing_key=key)
    if not (await keyed.probe()).reachable:
        pytest.skip("Redis indisponivel")
    forger = RedisEventBus(redis_url_of(settings), keys=keys)
    signer = RedisEventBus(redis_url_of(settings), keys=keys, signing_key=key)
    try:
        await forger.publish("heartbeat", {"state": "RUNNING", "heartbeat_at": "2026-01-01T00:00:00+00:00"})
        await forger.close()
        assert (await keyed.probe()).latest == {}
        assert keyed.rejections.total >= 1

        await signer.publish("heartbeat", {"state": "RUNNING", "heartbeat_at": "2026-01-01T00:00:00+00:00"})
        await signer.close()
        assert (await keyed.probe()).latest["heartbeat"]["state"] == "RUNNING"
    finally:
        await keyed.close()


async def test_redis_envelopes_survive_a_runner_crash_between_pop_and_write(settings: Any) -> None:
    from datetime import UTC, datetime

    from engine.runner.paper_events import RedisKeys
    from tests.integration.api.runner_support import redis_url_of

    keys = RedisKeys("cryptoinvest:test:atleastonce")
    key = COMMAND_KEY.encode()
    source = RedisCommandSource.from_url(redis_url_of(settings), key, keys=keys)
    now = datetime.now(UTC)
    try:
        await source._redis.delete(keys.commands, keys.processing, keys.nonce("emergency0007"))
    except Exception:
        await source.close()
        pytest.skip("Redis indisponivel")
    command = Command(name=CommandName.EMERGENCY_STOP, operator="ana", reason="teste", issued_at=now)
    envelope = sign_command(key, command, command_id="emergency0007", signed_at=now)
    await source._redis.rpush(keys.commands, envelope)
    try:
        [first] = await source.poll(now)
        assert first.command is not None
        restarted = RedisCommandSource.from_url(redis_url_of(settings), key, keys=keys)
        [again] = await restarted.poll(now)
        assert again.command_id == "emergency0007"
        assert await restarted.commit(again) is True
        assert await restarted.poll(now) == []
        await restarted.close()
    finally:
        await source._redis.delete(keys.commands, keys.processing, keys.nonce("emergency0007"))
        await source.close()
