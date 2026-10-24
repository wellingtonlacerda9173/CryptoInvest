"""QA: sondas de seguranca/robustez dos endpoints de controle, configuracao e leitura (xfail strict = bug aberto)."""

import asyncio
from typing import Any

import pytest
from sqlalchemy import text

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

__all__ = ["fake_link", "module_db_url", "module_sessions", "rh", "rh_admin", "rh_reader"]


async def _status(session: Session, url: str, **kw: Any) -> int:
    try:
        return (await session.get(url, **kw)).status_code
    except Exception:  # excecao nao tratada no app: ASGITransport a propaga (equivale a HTTP 500)
        return 500


@pytest.mark.parametrize("url", ["/control/commands", "/backtests"])
@pytest.mark.parametrize("cursor", ["lixo", "2026-01-01T00:00:00|x", "|", "9999-99-99"])
async def test_malformed_cursor_is_a_client_error(
    rh: Harness, rh_reader: Session, url: str, cursor: str
) -> None:
    assert await _status(rh_reader, url, params={"cursor": cursor}) in {400, 422}


@pytest.mark.parametrize(
    "value",
    [
        {"risk_per_trade": "NaN"},
        {"risk_per_trade": "Infinity"},
        {"risk_per_trade": -0.001},
        {"risk_per_trade": 0},
        {"risk_per_trade": "0.011"},
        {"risk_per_trade": 0.0100000001},
        {"risk_per_trade": True},
        {"risk_per_trade": [0.001]},
        {"risk_per_trade": {"a": 1}},
        {"max_open_positions": 2.5},
        {"max_open_positions": "3"},
        {"max_open_positions": 3},
        {"max_daily_loss": "0.0501"},
        {"max_drawdown": 1e9},
        {"max_total_exposure_fraction": "0.81"},
        {"pair_pause_hours": 23},
        {"min_reward_risk": "1.99"},
        {"consecutive_loss_limit": 3},
        {"allowed_assets": "BTC"},
        {"allowed_assets": ["btc"]},
        {"blocked_assets": [1]},
        {"quote_asset": "usdt; DROP"},
        {"max_entry_deviation": "0.021"},
        {"taker_fee_rate": "0.011"},
        {"max_quote_age_seconds": 301},
        {"__proto__": 1},
    ],
)
async def test_server_ceilings_reject_bad_values_and_save_nothing(
    rh: Harness, rh_admin: Session, value: dict[str, Any]
) -> None:
    before = (await rh_admin.get("/settings/history", params={"limit": 100})).json()["items"]
    version = ((await rh_admin.get("/settings")).json()["version"] or {}).get("id") or 0
    response = await rh_admin.send(
        "PUT",
        "/settings/risk",
        json={
            "value": value,
            "reason": "sonda de teto",
            "acknowledge_risk_increase": True,
            "expected_version": version,
        },
    )
    assert any(next(iter(value)) in e["loc"] for e in response.json()["errors"]), response.text[:200]
    assert response.status_code == 422, (value, response.status_code, response.text[:200])
    after = (await rh_admin.get("/settings/history", params={"limit": 100})).json()["items"]
    assert len(after) == len(before)


async def test_two_simultaneous_puts_never_lose_an_update(
    rh: Harness, rh_admin: Session, fake_link: FakeLink
) -> None:
    fake_link.latest["heartbeat"] = status_payload(now=rh.clock())
    current = (await rh_admin.get("/settings")).json()
    version = (current["version"] or {}).get("id") or 0
    bodies = [
        {"value": {"risk_per_trade": "0.0031"}, "reason": "A concorrente", "expected_version": version},
        {"value": {"max_daily_loss": "0.0091"}, "reason": "B concorrente", "expected_version": version},
    ]
    results = await asyncio.gather(*(rh_admin.send("PUT", "/settings/risk", json=b) for b in bodies))
    codes = sorted(r.status_code for r in results)
    assert codes == [200, 409], codes
    doc = (await rh_admin.get("/settings")).json()["document"]["risk"]
    changed = [doc["risk_per_trade"] == "0.0031", doc["max_daily_loss"] == "0.0091"]
    assert sum(changed) == 1


@pytest.mark.parametrize(
    "q",
    [
        "' OR 1=1 --",
        "%",
        "_",
        "\\",
        "a" * 60,
        "\x00",
        "'; DROP TABLE trades; --",
    ],
)
async def test_search_filters_are_parameterised(rh: Harness, rh_reader: Session, q: str) -> None:
    for url in ("/trades", "/trades/export"):
        assert await _status(rh_reader, url, params={"q": q}) in {200, 422}
    assert await _status(rh_reader, "/trades", params={"strategy": q}) in {200, 422}
    async with rh.sessions() as db:
        assert await db.scalar(text("SELECT to_regclass('public.trades')")) is not None


async def test_live_gate_cache_is_not_unbounded_for_unknown_run_ids(rh: Harness, rh_reader: Session) -> None:
    before = len(rh.container.gate_cache)
    for i in range(40):
        assert (await rh_reader.get("/live-gate", params={"run_id": f"inventado-{i}"})).status_code == 200
    assert len(rh.container.gate_cache) - before < 40, "gate_cache cresce sem limite com run_id arbitrario"


async def test_live_gate_for_a_run_that_does_not_exist_is_not_reported_as_eligible(
    rh: Harness, rh_reader: Session
) -> None:
    body = (await rh_reader.get("/live-gate", params={"run_id": "nao-existe"})).json()
    assert body["approved"] is False
    assert body["eligible_for_live"] is False, "run inexistente aparece como 'Elegivel' (criterio met)"


async def test_hard_limit_text_for_reward_risk_is_not_misleading(rh: Harness, rh_reader: Session) -> None:
    limits = (await rh_reader.get("/settings")).json()["hard_limits"]
    text_ = next(item["text"] for item in limits if item["path"] == "risk.min_reward_risk")
    assert "abaixo de 1 para" not in text_, text_


@pytest.mark.parametrize(
    "path", ["/control/pause", "/control/stop", "/control/emergency-stop", "/live/activate", "/settings/risk"]
)
async def test_writes_without_csrf_token_are_refused_even_for_admin(
    rh: Harness, rh_admin: Session, path: str
) -> None:
    method = "PUT" if path.startswith("/settings") else "POST"
    response = await rh_admin.client.request(method, path, json={"reason": "sem csrf", "value": {}})
    assert response.status_code in {401, 403}


async def test_unknown_fields_in_control_bodies_are_refused_and_create_no_command(
    rh: Harness, rh_admin: Session
) -> None:
    """Antes um `liquidat: true` com typo virava uma parada SEM venda e respondia 202: agora e 422."""
    before = (await rh_admin.get("/control/commands")).json()["items"]
    response = await rh_admin.send(
        "POST", "/control/emergency-stop", json={"liquidat": True, "reason": "typo nao passa"}
    )
    assert response.status_code == 422
    assert (await rh_admin.get("/control/commands")).json()["items"] == before


async def test_response_bodies_never_contain_hashes_of_credentials_or_keys(
    rh: Harness, rh_admin: Session
) -> None:
    for url in (
        "/settings",
        "/settings/history",
        "/control/commands",
        "/status",
        "/onboarding",
        "/backtests",
    ):
        body = (await rh_admin.get(url)).text.lower()
        for needle in ("password", "totp", "secret", "hmac", "api_secret", "session_token"):
            assert needle not in body, (url, needle)


async def test_activate_live_with_reader_and_admin_never_changes_mode(rh: Harness, rh_admin: Session) -> None:
    for body in (
        {},
        {"phrase": "OPERAR COM DINHEIRO REAL"},
        {"phrase": "OPERAR COM DINHEIRO REAL", "code": "000000"},
    ):
        response = await rh_admin.send("POST", "/live/activate", json=body)
        assert response.status_code in {409, 401, 403}
        assert response.status_code != 204
    status = (await rh_admin.get("/status")).json()
    assert status["mode"] == "paper" and status["live"]["live_available"] is False


@pytest.mark.parametrize("code", ["stale", "future"])
async def test_an_emergency_stop_discarded_for_clock_reasons_is_resent_not_given_up(
    rh: Harness, rh_admin: Session, fake_link: FakeLink, code: str
) -> None:
    from api.services.commands import CommandService

    fake_link.latest["heartbeat"] = status_payload(now=rh.clock())
    fake_link.signed_now = rh.clock()
    body = (await rh_admin.send("POST", "/control/emergency-stop", json={})).json()
    fake_link.acks[body["id"]] = {"result": "discarded", "detail": code}
    await CommandService(rh.container).reconcile()
    final = (await rh_admin.get(f"/control/commands/{body['id']}")).json()
    assert final["state"] != "discarded", "o 'Parar tudo' ficou descartado e nunca mais sera reenviado"


async def test_two_concurrent_first_versions_cannot_both_become_roots(harness: Harness) -> None:
    from engine.application.trading_config import default_document
    from engine.infrastructure.persistence.config_repository import ConfigConflictError, ConfigRepository

    async def first(tag: str) -> str:
        async with harness.sessions.begin() as db:
            try:
                await ConfigRepository(db).append(
                    default_document(),
                    actor=tag,
                    reason="primeira",
                    activation="immediate",
                    risk_increase=False,
                    sections=["risk"],
                    diff=[],
                    expected_parent=None,
                )
            except ConfigConflictError:
                return "conflict"
            await asyncio.sleep(0.3)  # segura a transacao aberta: as duas leem 'sem versao'
        return "ok"

    results = await asyncio.gather(first("a"), first("b"))
    assert sorted(results) == ["conflict", "ok"], results
