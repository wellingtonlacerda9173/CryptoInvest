"""Configuracoes versionadas: validacao por campo, tetos rigidos, aumento de risco, efeito e auditoria."""

import json
from decimal import Decimal
from itertools import pairwise
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from api.services.settings_service import RESTART_RULE
from engine.application.trading_config import ConfigDocument
from engine.runner.command_channel import ReplayGuard, verify_envelope
from tests.integration.api.conftest import Harness, Session
from tests.integration.api.runner_support import (
    COMMAND_KEY,
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


async def alive_as_saved(rh: Harness, admin: Session, link: FakeLink, **overrides: Any) -> None:
    """Robo ligado que aplicou tudo o que esta salvo: o risco em vigor que ele reporta = ultimo documento."""
    risk = (await current(admin))["document"]["risk"]
    alive(rh, link, risk=risk, **overrides)


async def current(admin: Session) -> dict[str, Any]:
    return (await admin.get("/settings")).json()  # type: ignore[no-any-return]


async def put(admin: Session, section: str, value: dict[str, Any], **extra: Any) -> Any:
    if "expected_version" not in extra:
        version = (await current(admin))["version"]
        extra["expected_version"] = 0 if version is None else version["id"]
    body = {"value": value, "reason": "ajuste de teste", **extra}
    return await admin.send("PUT", f"/settings/{section}", json=body)


async def history(admin: Session) -> list[dict[str, Any]]:
    return (await admin.get("/settings/history", params={"limit": 100})).json()["items"]  # type: ignore[no-any-return]


# --------------------------------------------------------------------------- leitura


async def test_reader_and_admin_read_the_settings_with_defaults_limits_and_field_specs(
    rh: Harness, rh_admin: Session, rh_reader: Session
) -> None:
    admin_view, reader_view = await current(rh_admin), await current(rh_reader)
    assert admin_view["can_edit"] is True and reader_view["can_edit"] is False
    assert admin_view["document"] == reader_view["document"] == admin_view["defaults"]
    assert admin_view["document"]["risk"]["risk_per_trade"] == "0.005"
    assert set(admin_view["document"]) == {"pairs", "strategies", "risk", "ai"}
    assert admin_view["document"]["ai"] == {
        "enabled": False,
        "model": "claude-haiku-5-5",
        "monthly_budget_usd": "20",
        "timeout_seconds": 8.0,
    }
    assert (
        admin_view["effective"]["restart_rule"] == RESTART_RULE
        and admin_view["effective"]["runner_connected"] is False
    )
    ceilings = {h["path"]: h for h in admin_view["hard_limits"]}
    assert ceilings["risk.risk_per_trade"]["text"] == "Risco por operação: no máximo 1,00%"
    assert (
        ceilings["risk.max_open_positions"]["value"] == 2
        and ceilings["risk.pair_pause_hours"]["kind"] == "min"
    )
    fields = {f["path"]: f for f in admin_view["fields"]["risk"]}
    assert (
        fields["risk.risk_per_trade"]["hard_limit"] is True
        and fields["risk.risk_per_trade"]["maximum"] == 0.01
    )
    assert admin_view["version"] is None and admin_view["capital_reference"] > 0
    assert (await rh_admin.get("/settings/history")).json() == {"items": [], "next_cursor": None}


async def test_anonymous_cannot_read_and_reader_cannot_write(rh: Harness, rh_reader: Session) -> None:
    anonymous = rh.client()
    assert (await anonymous.get("/settings")).status_code == 401
    await anonymous.aclose()
    assert (await put(rh_reader, "risk", {"risk_per_trade": "0.004"})).status_code == 403
    assert (
        await rh_reader.send("POST", "/settings/validate", json={"section": "risk", "value": {}})
    ).status_code == 403


async def test_saving_needs_a_recent_two_factor_and_the_csrf_token(rh: Harness, rh_admin: Session) -> None:
    no_csrf = await rh_admin.client.put(
        "/settings/risk", json={"value": {"risk_per_trade": "0.004"}, "reason": "sem csrf"}
    )
    assert no_csrf.status_code == 403
    rh.clock.advance(minutes=6)
    stale = await put(rh_admin, "risk", {"risk_per_trade": "0.004"})
    assert stale.status_code == 403 and stale.json()["type"].endswith("fresh-auth-required")
    assert await history(rh_admin) == []


# --------------------------------------------------------------------------- validar sem salvar


async def test_validate_reports_errors_by_field_in_plain_portuguese_without_saving(
    rh: Harness, rh_admin: Session
) -> None:
    before = await history(rh_admin)
    response = await rh_admin.send(
        "POST",
        "/settings/validate",
        json={
            "section": "risk",
            "value": {"risk_per_trade": "0.02", "max_open_positions": 5, "nao_existe": 1},
        },
    )
    body = response.json()
    assert response.status_code == 200 and body["valid"] is False and body["changes"] == []
    errors = {e["path"]: e["message"] for e in body["errors"]}
    assert errors["risk.nao_existe"] == "Campo desconhecido."
    assert set(errors) == {"risk.nao_existe"}
    second = (
        await rh_admin.send(
            "POST",
            "/settings/validate",
            json={"section": "risk", "value": {"risk_per_trade": "0.02", "max_open_positions": 5}},
        )
    ).json()
    errors = {e["path"]: e["message"] for e in second["errors"]}
    assert "0.01" in errors["risk.risk_per_trade"] and "teto de segurança" in errors["risk.risk_per_trade"]
    assert "máximo permitido é 2" in errors["risk.max_open_positions"]
    assert "0.02" not in json.dumps(second["errors"])
    assert await history(rh_admin) == before


async def test_validate_flags_each_change_that_raises_risk_with_before_now_and_cost(
    rh: Harness, rh_admin: Session, fake_link: FakeLink
) -> None:
    await alive_as_saved(rh, rh_admin, fake_link)
    response = await rh_admin.send(
        "POST",
        "/settings/validate",
        json={
            "section": "risk",
            "value": {"risk_per_trade": "0.008", "max_open_positions": 1, "max_daily_loss": "0.02"},
        },
    )
    body = response.json()
    assert body["valid"] is True and body["increases_risk"] and body["requires_acknowledgement"]
    changes = {c["path"]: c for c in body["changes"]}
    risk = changes["risk.risk_per_trade"]
    assert (risk["effect"], risk["increases_risk"], risk["applies"]) == ("increase", True, "next_start")
    assert risk["before_text"] == "0,50%" and risk["after_text"] == "0,80%"
    assert (
        "Cada operação perdida pode custar até 0,80% do saldo" in risk["cost_text"]
        and "antes eram 0,50%" in risk["cost_text"]
    )
    assert changes["risk.max_daily_loss"]["increases_risk"] is True
    fewer = changes["risk.max_open_positions"]
    assert (fewer["effect"], fewer["increases_risk"], fewer["applies"]) == ("decrease", False, "now")
    assert body["activation"] == "next_start" and "Parte vale agora" in body["activation_text"]
    assert len(body["config_hash"]) == 64


async def test_validating_a_pure_reduction_is_immediate_and_a_noop_changes_nothing(
    rh: Harness, rh_admin: Session, fake_link: FakeLink
) -> None:
    await alive_as_saved(rh, rh_admin, fake_link)
    reduction = (
        await rh_admin.send(
            "POST", "/settings/validate", json={"section": "risk", "value": {"risk_per_trade": "0.003"}}
        )
    ).json()
    assert reduction["activation"] == "immediate" and not reduction["requires_acknowledgement"]
    noop = (
        await rh_admin.send(
            "POST", "/settings/validate", json={"section": "risk", "value": {"risk_per_trade": "0.0050"}}
        )
    ).json()
    assert noop["valid"] and noop["changes"] == [] and noop["activation"] == "none"


# --------------------------------------------------------------------------- gravar


async def test_reduction_is_saved_as_a_version_and_sent_to_the_robot_immediately(
    rh: Harness, rh_admin: Session, fake_link: FakeLink
) -> None:
    await alive_as_saved(rh, rh_admin, fake_link)
    fake_link.signed_now = rh.clock()
    response = await put(rh_admin, "risk", {"risk_per_trade": "0.003", "max_open_positions": 1})
    assert response.status_code == 200, response.text
    body = response.json()
    version = body["version"]
    assert version["actor"] == rh_admin.username and version["reason"] == "ajuste de teste"
    assert (
        version["activation"] == "immediate"
        and version["risk_increase"] is False
        and version["sections"] == ["risk"]
    )
    assert body["activation"] == "immediate" and sorted(body["applied_now"]) == [
        "Posições abertas ao mesmo tempo",
        "Risco por operação",
    ]
    assert body["pending_restart"] == []
    assert {c["path"] for c in version["changes"]} == {"risk.risk_per_trade", "risk.max_open_positions"}

    command = body["command"]
    assert command["name"] == "apply-config" and command["state"] == "delivered"
    outcome = verify_envelope(fake_link.pushed[-1], COMMAND_KEY.encode(), fake_link.signed_now, ReplayGuard())
    assert outcome.command is not None
    assert outcome.command.params["risk"] == {"risk_per_trade": "0.003", "max_open_positions": 1}
    assert outcome.command.params["config_hash"] == version["config_hash"]

    document = (await current(rh_admin))["document"]
    assert document["risk"]["risk_per_trade"] == "0.003" and document["risk"]["risk_per_trade"] != "0.005"
    assert ConfigDocument.model_validate(document).digest() == version["config_hash"]
    audit = (await rh_admin.get("/audit", params={"action": "config."})).json()["items"]
    updated = [e for e in audit if e["action"] == "config.updated"]
    assert updated and updated[0]["actor"] == rh_admin.username
    assert (
        updated[0]["details"]["config_hash"] == version["config_hash"]
        and updated[0]["details"]["reason"] == "ajuste de teste"
    )
    assert {c["path"] for c in updated[0]["details"]["changes"]} == {
        "risk.risk_per_trade",
        "risk.max_open_positions",
    }


async def test_an_increase_needs_the_second_click_and_only_waits_for_the_next_start(
    rh: Harness, rh_admin: Session, fake_link: FakeLink
) -> None:
    await alive_as_saved(rh, rh_admin, fake_link)
    fake_link.signed_now = rh.clock()
    pushed_before = len(fake_link.pushed)
    versions_before = len(await history(rh_admin))
    unconfirmed = await put(rh_admin, "risk", {"risk_per_trade": "0.007"})
    assert unconfirmed.status_code == 409 and unconfirmed.json()["type"].endswith("risk-increase-unconfirmed")
    assert (
        "antes 0,30%, agora 0,70%" in unconfirmed.json()["detail"]
        and "Cada operação perdida" in unconfirmed.json()["detail"]
    )
    assert len(await history(rh_admin)) == versions_before

    confirmed = await put(rh_admin, "risk", {"risk_per_trade": "0.007"}, acknowledge_risk_increase=True)
    body = confirmed.json()
    assert confirmed.status_code == 200 and body["activation"] == "next_start" and body["command"] is None
    assert body["version"]["risk_increase"] is True and body["version"]["activation"] == "next_start"
    assert body["pending_restart"] == ["Risco por operação"] and "próximo início" in body["activation_text"]
    assert len(fake_link.pushed) == pushed_before
    refusals = [
        e
        for e in (await rh_admin.get("/audit", params={"action": "config."})).json()["items"]
        if e["action"] == "config.refused"
    ]
    assert refusals and refusals[0]["result"] == "denied"


async def test_a_mixed_change_applies_only_the_reduction_now(
    rh: Harness, rh_admin: Session, fake_link: FakeLink
) -> None:
    await alive_as_saved(rh, rh_admin, fake_link)
    fake_link.signed_now = rh.clock()
    body = (
        await put(
            rh_admin,
            "risk",
            {"max_drawdown": "0.04", "max_daily_loss": "0.02"},
            acknowledge_risk_increase=True,
        )
    ).json()
    assert body["activation"] == "next_start" and body["applied_now"] == ["Queda máxima do saldo (drawdown)"]
    assert body["pending_restart"] == ["Perda diária máxima"]
    outcome = verify_envelope(fake_link.pushed[-1], COMMAND_KEY.encode(), fake_link.signed_now, ReplayGuard())
    assert outcome.command is not None and outcome.command.params["risk"] == {"max_drawdown": "0.04"}


async def test_values_above_the_hard_ceiling_are_rejected_by_the_server_and_nothing_is_saved(
    rh: Harness, rh_admin: Session, fake_link: FakeLink
) -> None:
    versions = len(await history(rh_admin))
    pushed = len(fake_link.pushed)
    for field, value in (
        ("risk_per_trade", "0.011"),
        ("max_daily_loss", "0.06"),
        ("max_drawdown", "0.06"),
        ("max_open_positions", 3),
        ("consecutive_loss_limit", 3),
        ("pair_pause_hours", 23),
        ("max_total_exposure_fraction", "0.9"),
        ("min_reward_risk", "1.2"),
    ):
        response = await put(rh_admin, "risk", {field: value}, acknowledge_risk_increase=True)
        body = response.json()
        assert response.status_code == 422, (field, response.text)
        assert [e["loc"] for e in body["errors"]] == [["risk", field]], body
    assert len(await history(rh_admin)) == versions and len(fake_link.pushed) == pushed


async def test_omitted_fields_keep_their_values_and_typos_are_errors_not_defaults(
    rh: Harness, rh_admin: Session
) -> None:
    before = (await current(rh_admin))["document"]["risk"]
    bad = await put(rh_admin, "risk", {"risk_per_trad": "0.001"})
    assert bad.status_code == 422 and bad.json()["errors"][0]["loc"] == ["risk", "risk_per_trad"]
    ok = await put(rh_admin, "risk", {"max_spread": "0.001"})
    assert ok.status_code == 200
    after = (await current(rh_admin))["document"]["risk"]
    assert after["max_spread"] == "0.001" and {k: v for k, v in after.items() if k != "max_spread"} == {
        k: v for k, v in before.items() if k != "max_spread"
    }


async def test_saving_without_changes_and_with_a_stale_version_are_conflicts(
    rh: Harness, rh_admin: Session
) -> None:
    same = (await current(rh_admin))["document"]["risk"]
    assert (await put(rh_admin, "risk", {"max_spread": same["max_spread"]})).status_code == 409
    latest = (await current(rh_admin))["version"]["id"]
    stale = await put(rh_admin, "risk", {"max_spread": "0.0005"}, expected_version=latest - 1)
    assert stale.status_code == 409 and "mudou enquanto você editava" in stale.json()["detail"]
    fresh = await put(rh_admin, "risk", {"max_spread": "0.0005"}, expected_version=latest)
    assert fresh.status_code == 200
    short_reason = await rh_admin.send(
        "PUT", "/settings/risk", json={"value": {"max_spread": "0.0004"}, "reason": "x"}
    )
    assert short_reason.status_code == 422


async def test_versions_are_append_only_hash_chained_and_immutable_in_the_database(
    rh: Harness, rh_admin: Session
) -> None:
    versions = await history(rh_admin)
    assert len(versions) >= 3
    ordered = list(reversed(versions))
    assert ordered[0]["parent_id"] is None
    for parent, child in pairwise(ordered):
        assert child["parent_id"] == parent["id"] and child["chain_hash"] != parent["chain_hash"]
    assert len({v["chain_hash"] for v in versions}) == len(versions)
    async with rh.sessions() as db:
        for statement in (
            "UPDATE config_versions SET reason = 'adulterado'",
            "DELETE FROM config_versions",
            "TRUNCATE config_versions",
        ):
            with pytest.raises(DBAPIError):
                await db.execute(text(statement))
            await db.rollback()


async def test_history_is_paginated_and_readable_by_the_reader(
    rh: Harness, rh_admin: Session, rh_reader: Session
) -> None:
    page = (await rh_reader.get("/settings/history", params={"limit": 2})).json()
    assert len(page["items"]) == 2 and page["next_cursor"] == page["items"][-1]["id"]
    nxt = (
        await rh_reader.get("/settings/history", params={"limit": 50, "cursor": page["next_cursor"]})
    ).json()
    assert all(v["id"] < page["next_cursor"] for v in nxt["items"])


# --------------------------------------------------------------------------- outras secoes


async def test_pairs_strategies_and_ai_sections(rh: Harness, rh_admin: Session, fake_link: FakeLink) -> None:
    await alive_as_saved(rh, rh_admin, fake_link)
    fake_link.signed_now = rh.clock()
    pushed = len(fake_link.pushed)
    more = await put(rh_admin, "pairs", {"symbols": ["BTC/USDT", "ETH/USDT", "SOL/USDT"]})
    assert more.status_code == 409
    more = await put(
        rh_admin, "pairs", {"symbols": ["BTC/USDT", "ETH/USDT", "SOL/USDT"]}, acknowledge_risk_increase=True
    )
    assert more.json()["activation"] == "next_start" and more.json()["command"] is None
    fewer = (await put(rh_admin, "pairs", {"symbols": ["BTC/USDT"]})).json()
    assert fewer["activation"] == "next_start" and fewer["pending_restart"] == ["Pares"]
    bad_pair = await put(rh_admin, "pairs", {"symbols": ["btc"]})
    assert bad_pair.status_code == 422

    strategies = await put(rh_admin, "strategies", {"elephant": {"rsi_min": 40.0}, "min_confidence": 0.7})
    assert strategies.status_code == 409
    saved = await put(
        rh_admin,
        "strategies",
        {"elephant": {"rsi_min": 40.0}, "min_confidence": 0.7},
        acknowledge_risk_increase=True,
    )
    assert saved.status_code == 200 and "strategies.elephant.rsi_min" in {
        c["path"] for c in saved.json()["version"]["changes"]
    }
    document = (await current(rh_admin))["document"]["strategies"]
    assert document["elephant"]["rsi_min"] == 40.0 and document["elephant"]["rsi_max"] == 75.0
    none_enabled = await put(rh_admin, "strategies", {"elephant_enabled": False, "ema_enabled": False})
    assert none_enabled.status_code == 422

    ai = (await put(rh_admin, "ai", {"enabled": True})).json()
    assert ai["activation"] == "next_start" and ai["command"] is None
    assert (await put(rh_admin, "ai", {"model": "modelo com espaco"})).status_code == 422
    assert len(fake_link.pushed) == pushed


async def test_effective_config_compares_the_running_version_with_the_latest(
    rh: Harness, rh_admin: Session, fake_link: FakeLink
) -> None:
    latest = (await current(rh_admin))["version"]
    alive(rh, fake_link, config_version=latest["id"], config_hash="a" * 32)
    effective = (await current(rh_admin))["effective"]
    assert (
        effective["matches_latest"] is True
        and effective["version_id"] == latest["id"]
        and effective["runner_connected"]
    )
    alive(rh, fake_link, config_version=latest["id"] - 1)
    assert (await current(rh_admin))["effective"]["matches_latest"] is False
    fake_link.latest.clear()
    assert (await current(rh_admin))["effective"]["matches_latest"] is None


async def test_no_settings_response_carries_secrets(rh: Harness, rh_admin: Session) -> None:
    raw = json.dumps([await current(rh_admin), await history(rh_admin)]).lower()
    for word in ("password", "secret", "api_key", "hmac", COMMAND_KEY):
        assert word not in raw
    assert Decimal("0.005") > 0
