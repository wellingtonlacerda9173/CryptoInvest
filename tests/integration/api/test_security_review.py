"""Sondas da revisao de seguranca da API: cada teste afirma o comportamento SEGURO esperado.

Testes marcados `xfail(strict=True, reason="BUG: ...")` comprovam um defeito; ao corrigir o codigo o xfail
estrito falha e obriga a remover a marca (o teste vira regressao).
"""

import asyncio
import uuid
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from sqlalchemy import select

from api.binance_keys import BinanceKeyVerifier, KeyVerificationError
from api.persistence.models import SessionRow, UserRow
from engine.infrastructure.persistence.models import AuditLogRow
from tests.api_support import GOOD_PASSWORD
from tests.integration.api.conftest import Harness, Session

pytestmark = pytest.mark.integration


async def _audit_actions(harness: Harness) -> list[str]:
    async with harness.sessions() as db:
        return [r.action for r in (await db.scalars(select(AuditLogRow).order_by(AuditLogRow.id))).all()]


async def _serve_raw(body: Any, status: int = 200) -> TestServer:
    async def handler(_: web.Request) -> web.Response:
        return web.json_response(body, status=status)

    app = web.Application()
    app.router.add_get("/sapi/v1/account/apiRestrictions", handler)
    server = TestServer(app)
    await server.start_server()
    return server


# ------------------------------------------------- chaves Binance: falha fechada


async def test_verifier_fails_closed_when_withdraw_flag_is_missing() -> None:
    server = await _serve_raw({"enableReading": True, "enableSpotAndMarginTrading": True})
    try:
        verifier = BinanceKeyVerifier(str(server.make_url("")))
        with pytest.raises(KeyVerificationError):
            await verifier.verify("AKtestKey0123456789abcdef", "SECRETtest0123456789abcdef")
    finally:
        await server.close()


async def test_verifier_rejects_non_object_payload_with_domain_error() -> None:
    server = await _serve_raw([1, 2, 3])
    try:
        verifier = BinanceKeyVerifier(str(server.make_url("")))
        with pytest.raises(KeyVerificationError):
            await verifier.verify("AKtestKey0123456789abcdef", "SECRETtest0123456789abcdef")
    finally:
        await server.close()


# ------------------------------------------------- autorizacao


async def test_creating_admin_with_stale_session_requires_fresh_auth(
    harness: Harness, admin: Session
) -> None:
    harness.clock.advance(minutes=10)
    response = await admin.send(
        "POST",
        "/users",
        json={"username": f"backdoor.{uuid.uuid4().hex[:6]}", "password": GOOD_PASSWORD, "role": "admin"},
    )
    assert response.status_code == 403


async def test_role_change_with_stale_session_requires_fresh_auth(harness: Harness, admin: Session) -> None:
    reader_name = await harness.create_user("reader")
    async with harness.sessions() as db:
        target = await db.scalar(select(UserRow).where(UserRow.username == reader_name))
        assert target is not None
        target_id = target.id
    harness.clock.advance(minutes=10)
    response = await admin.send("PATCH", f"/users/{target_id}", json={"role": "admin"})
    assert response.status_code == 403


# A corrida do ultimo admin tem sondagem deterministica em test_qa_probes.py (esta versao dependia de o banco
# compartilhado estar sem outros admins e dava XPASS).


# ------------------------------------------------- lockout / throttle


async def test_anonymous_attacker_cannot_lock_out_legitimate_admin(harness: Harness) -> None:
    username = await harness.create_user("admin")
    (await harness.login(username)).client  # noqa: B018 - enrola o 2FA
    attacker = harness.client(ip="192.0.2.66")
    for _ in range(6):
        await attacker.post("/auth/login", json={"username": username, "password": "errada errada 123!"})
    blocked = await attacker.post("/auth/login", json={"username": username, "password": GOOD_PASSWORD})
    assert blocked.status_code == 429, "o par (usuario, IP) do atacante fica bloqueado"
    harness.clock.advance(seconds=1)
    victim = await harness.login(username, ip="198.51.100.20")
    assert (await victim.get("/auth/me")).status_code == 200


async def test_concurrent_burst_cannot_exceed_ip_threshold(harness: Harness) -> None:
    client = harness.client()

    async def attempt(i: int) -> int:
        r = await client.post(
            "/auth/login", json={"username": f"ghost{i}.{uuid.uuid4().hex[:6]}", "password": "x" * 14}
        )
        return r.status_code

    codes = await asyncio.gather(*(attempt(i) for i in range(60)))
    assert codes.count(401) <= 20, codes.count(401)


# ------------------------------------------------- auditoria de negacoes


async def test_security_denials_are_audited(harness: Harness, admin: Session) -> None:
    before = len(await _audit_actions(harness))
    await admin.client.post("/auth/logout")  # sem CSRF
    harness.clock.advance(minutes=10)
    await admin.send("POST", "/credentials", json={"label": "x", "api_key": "k" * 20, "api_secret": "s" * 20})
    await admin.send("DELETE", f"/sessions/{uuid.uuid4()}")
    after = await _audit_actions(harness)
    assert len(after) - before >= 3, after[before:]


# ------------------------------------------------- CSRF / content-type / cookies


async def test_form_encoded_and_text_plain_bodies_are_not_parsed_as_json(
    harness: Harness, admin: Session
) -> None:
    body = '{"username": "csrf.victim", "password": "cavalo azul voa sobre 7 montanhas!", "role": "admin"}'
    for content_type in (
        "text/plain",
        "application/x-www-form-urlencoded",
        "multipart/form-data; boundary=x",
    ):
        r = await admin.client.post(
            "/users", content=body, headers={**admin.headers, "Content-Type": content_type}
        )
        assert r.status_code in {415, 422, 400}, (content_type, r.status_code)


async def test_mutating_request_without_csrf_header_but_with_cookie_is_rejected(
    harness: Harness, admin: Session
) -> None:
    r = await admin.client.post("/auth/2fa/recovery-codes")
    assert r.status_code == 403
    cookie = admin.client.cookies.get(harness.settings.csrf_cookie_name)
    # cookie CSRF correto, mas sem o header: double-submit exige os dois
    r2 = await admin.client.post("/auth/logout", headers={"X-CSRF-Token": ""}, cookies={})
    assert r2.status_code == 403 and cookie


async def test_csrf_token_of_another_session_is_rejected(
    harness: Harness, admin: Session, reader: Session
) -> None:
    r = await admin.client.post(
        "/auth/logout",
        headers={"X-CSRF-Token": reader.csrf},
        cookies={harness.settings.csrf_cookie_name: reader.csrf},
    )
    assert r.status_code == 403


async def test_session_cookie_attributes(harness: Harness) -> None:
    username = await harness.create_user("reader")
    client = harness.client()
    r = await client.post("/auth/login", json={"username": username, "password": GOOD_PASSWORD})
    cookies = {c.split("=", 1)[0]: c.lower() for c in r.headers.get_list("set-cookie")}
    session = cookies[harness.settings.session_cookie_name]
    csrf = cookies[harness.settings.csrf_cookie_name]
    assert (
        "httponly" in session
        and "samesite=strict" in session
        and "path=/" in session
        and "domain" not in session
    )
    assert "httponly" not in csrf and "samesite=strict" in csrf and "domain" not in csrf


# ------------------------------------------------- sessao pendente x rotas protegidas


async def test_pending_session_cannot_reach_any_protected_route(harness: Harness) -> None:
    username = await harness.create_user("admin")
    first = await harness.login(username)
    await first.aclose()
    client = harness.client()
    step = await client.post("/auth/login", json={"username": username, "password": GOOD_PASSWORD})
    assert step.json()["stage"] == "totp"
    headers = {"X-CSRF-Token": step.json()["csrf_token"]}
    for method, path in [
        ("GET", "/auth/me"),
        ("GET", "/sessions"),
        ("GET", "/users"),
        ("GET", "/audit"),
        ("GET", "/credentials"),
        ("POST", "/auth/reauth"),
        ("POST", "/auth/logout"),
        ("POST", "/auth/password"),
        ("DELETE", "/sessions"),
    ]:
        r = await client.request(
            method, path, headers=headers, json={"code": "123456"} if method == "POST" else None
        )
        assert r.status_code == 401, (method, path, r.status_code)


async def test_concurrent_totp_replay_across_pending_sessions_yields_single_success(harness: Harness) -> None:
    username = await harness.create_user("admin")
    first = await harness.login(username)
    await first.aclose()
    clients = [harness.client() for _ in range(4)]
    steps = [
        await c.post("/auth/login", json={"username": username, "password": GOOD_PASSWORD}) for c in clients
    ]
    harness.clock.advance(seconds=30)
    code = harness.code_for(username)
    results = await asyncio.gather(
        *(
            c.post("/auth/2fa/verify", json={"code": code}, headers={"X-CSRF-Token": s.json()["csrf_token"]})
            for c, s in zip(clients, steps, strict=True)
        )
    )
    assert sorted(r.status_code for r in results).count(200) == 1, [r.status_code for r in results]


async def test_concurrent_recovery_code_use_yields_single_success(harness: Harness) -> None:
    username = await harness.create_user("admin")
    first = await harness.login(username)
    code = first.recovery_codes[0]
    await first.aclose()
    clients = [harness.client() for _ in range(4)]
    steps = [
        await c.post("/auth/login", json={"username": username, "password": GOOD_PASSWORD}) for c in clients
    ]
    results = await asyncio.gather(
        *(
            c.post(
                "/auth/2fa/verify",
                json={"recovery_code": code},
                headers={"X-CSRF-Token": s.json()["csrf_token"]},
            )
            for c, s in zip(clients, steps, strict=True)
        )
    )
    assert [r.status_code for r in results].count(200) == 1


async def test_verified_session_rotates_token_and_old_pending_dies(harness: Harness) -> None:
    username = await harness.create_user("admin")
    first = await harness.login(username)
    await first.aclose()
    client = harness.client()
    step = await client.post("/auth/login", json={"username": username, "password": GOOD_PASSWORD})
    pending_cookie = client.cookies.get(harness.settings.session_cookie_name)
    harness.clock.advance(seconds=30)
    done = await client.post(
        "/auth/2fa/verify",
        json={"code": harness.code_for(username)},
        headers={"X-CSRF-Token": step.json()["csrf_token"]},
    )
    assert done.status_code == 200
    assert client.cookies.get(harness.settings.session_cookie_name) != pending_cookie
    attacker = harness.client()
    attacker.cookies.set(harness.settings.session_cookie_name, pending_cookie)
    assert (await attacker.get("/auth/me")).status_code == 401
    async with harness.sessions() as db:
        rows = (await db.scalars(select(SessionRow).where(SessionRow.revoked_reason == "rotated"))).all()
    assert rows


# ------------------------------------------------- IP / XFF


async def test_x_forwarded_for_is_ignored_by_default(harness: Harness) -> None:
    username = await harness.create_user("reader")
    client = harness.client()
    await client.post(
        "/auth/login",
        json={"username": username, "password": "errada errada 123!"},
        headers={"X-Forwarded-For": "6.6.6.6"},
    )
    async with harness.sessions() as db:
        rows = (await db.scalars(select(AuditLogRow).where(AuditLogRow.action == "auth.login.failed"))).all()
    row = next((r for r in rows if r.payload["details"].get("attempted_user") == username), None)
    assert row is not None and row.payload["ip"] == "203.0.113.9"
