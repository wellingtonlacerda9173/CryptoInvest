"""Sondas adicionais da revisao de seguranca (QA): cada teste afirma o comportamento SEGURO esperado.

`xfail(strict=True, reason="BUG: ...")` comprova um defeito; ao corrigir, o xfail estrito falha e obriga a
remover a marca (o teste vira regressao). Complementa `test_security_review.py`.
"""

import asyncio
import contextlib
import uuid
from typing import Any

import httpx
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from sqlalchemy import select

from api.app import create_app
from api.audit import SYSTEM_META, AuditService
from api.binance_keys import BinanceKeyVerifier, KeyVerificationError
from api.persistence.models import SessionRow, UserRow
from api.services.users import UserService
from engine.infrastructure.persistence.models import AuditLogRow
from tests.api_support import GOOD_PASSWORD, make_settings
from tests.integration.api.conftest import Harness, Session, fake_key

pytestmark = pytest.mark.integration


async def _actions(harness: Harness) -> list[str]:
    async with harness.sessions() as db:
        return [r.action for r in (await db.scalars(select(AuditLogRow).order_by(AuditLogRow.id))).all()]


# ----------------------------------------------------------------------------- CSRF com bytes nao ASCII


async def test_non_ascii_csrf_header_is_a_403_not_a_500(harness: Harness, admin: Session) -> None:
    response = await admin.client.post("/auth/logout", headers=[(b"x-csrf-token", b"\xe9\xe9\xe9")])
    assert response.status_code == 403


# ----------------------------------------------------------------------------- ultimo admin (corrida)


async def test_crossed_demotions_deterministic_interleaving_leave_an_admin(harness: Harness) -> None:
    name_a = await harness.create_user("admin")
    name_b = await harness.create_user("admin")
    async with harness.sessions() as db:
        rows = {
            u.username: u
            for u in (await db.scalars(select(UserRow).where(UserRow.username.in_([name_a, name_b])))).all()
        }
    a, b = rows[name_a], rows[name_b]
    # isola do restante do banco de teste: so estes dois sao admins ativos
    async with harness.sessions.begin() as db:
        for other in (await db.scalars(select(UserRow).where(UserRow.role == "admin"))).all():
            if other.id not in {a.id, b.id}:
                other.role = "reader"

    service = UserService(harness.container, AuditService(harness.sessions))
    barrier = asyncio.Barrier(2)
    original = UserService._active_admins

    async def counted_then_wait(self: UserService, db: Any) -> int:
        count = await original(self, db)
        # sem a trava dos admins as duas contam ANTES de qualquer commit; com ela a segunda nem chega aqui
        with contextlib.suppress(TimeoutError, asyncio.BrokenBarrierError):
            await asyncio.wait_for(barrier.wait(), 1.0)
        return count

    UserService._active_admins = counted_then_wait  # type: ignore[method-assign]
    try:
        meta = SYSTEM_META
        results = await asyncio.wait_for(
            asyncio.gather(
                service.update(
                    user_id=b.id, role="reader", disabled=None, actor_id=a.id, actor=a.username, meta=meta
                ),
                service.update(
                    user_id=a.id, role="reader", disabled=None, actor_id=b.id, actor=b.username, meta=meta
                ),
                return_exceptions=True,
            ),
            timeout=20,
        )
    finally:
        UserService._active_admins = original  # type: ignore[method-assign]
    async with harness.sessions() as db:
        remaining = (
            await db.scalars(select(UserRow).where(UserRow.role == "admin", UserRow.disabled.is_(False)))
        ).all()
    assert len(remaining) >= 1, results


# ----------------------------------------------------------------------------- IDOR de sessoes


async def test_cannot_revoke_a_session_of_another_user(
    harness: Harness, admin: Session, reader: Session
) -> None:
    async with harness.sessions() as db:
        admin_rows = (
            await db.scalars(
                select(SessionRow)
                .join(UserRow, UserRow.id == SessionRow.user_id)
                .where(UserRow.username == admin.username)
            )
        ).all()
    target = next(r for r in admin_rows if r.revoked_at is None and r.mfa_verified_at is not None)
    response = await reader.send("DELETE", f"/sessions/{target.id}")
    assert response.status_code == 404
    assert (await admin.get("/auth/me")).status_code == 200
    listing = await reader.get("/sessions")
    assert all(item["id"] != str(target.id) for item in listing.json())


# ----------------------------------------------------------------------------- auditoria de falhas na verificacao


async def test_failed_stored_key_verification_is_audited(harness: Harness, admin: Session) -> None:
    key, secret = fake_key()
    created = await admin.send(
        "POST",
        "/credentials",
        json={"label": f"k{uuid.uuid4().hex[:8]}", "api_key": key, "api_secret": secret},
    )
    assert created.status_code == 201, created.text
    before = len(await _actions(harness))
    harness.verifier.error = "A Binance recusou a chave."
    try:
        response = await admin.send("POST", f"/credentials/{created.json()['id']}/verify")
    finally:
        harness.verifier.error = None
    assert response.status_code == 422
    assert len(await _actions(harness)) > before


# ----------------------------------------------------------------------------- cookies em modo seguro


async def test_secure_mode_cookies_satisfy_host_prefix_rules(harness: Harness) -> None:
    settings = make_settings(cookie_secure=True)
    app = create_app(settings, sessions=harness.sessions, verifier=harness.verifier, clock=harness.clock)
    username = await harness.create_user("reader")
    transport = httpx.ASGITransport(app=app, client=("203.0.113.9", 4000))
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
        response = await client.post("/auth/login", json={"username": username, "password": GOOD_PASSWORD})
    cookies = {c.split("=", 1)[0]: c.lower() for c in response.headers.get_list("set-cookie")}
    for name in ("__Host-ci_session", "__Host-ci_csrf"):
        attrs = cookies[name]
        assert (
            "; secure" in attrs and "path=/" in attrs and "domain" not in attrs and "samesite=strict" in attrs
        )
    assert "httponly" in cookies["__Host-ci_session"] and "httponly" not in cookies["__Host-ci_csrf"]


# ----------------------------------------------------------------------------- verificador Binance


async def _serve(body: Any, status: int = 200) -> TestServer:
    async def handler(_: web.Request) -> web.Response:
        return web.json_response(body, status=status)

    app = web.Application()
    app.router.add_get("/sapi/v1/account/apiRestrictions", handler)
    server = TestServer(app)
    await server.start_server()
    return server


async def test_crlf_in_api_key_cannot_inject_headers_and_fails_with_domain_error() -> None:
    server = await _serve({"enableReading": True, "enableWithdrawals": False})
    try:
        verifier = BinanceKeyVerifier(str(server.make_url("")))
        with pytest.raises(KeyVerificationError):
            await verifier.verify("AKtest0123456789\r\nX-Evil: 1", "SECRETtest0123456789abcdef")
    finally:
        await server.close()


async def test_non_boolean_withdraw_flag_is_rejected() -> None:
    server = await _serve({"enableReading": True, "enableWithdrawals": None})
    try:
        verifier = BinanceKeyVerifier(str(server.make_url("")))
        with pytest.raises(KeyVerificationError):
            await verifier.verify("AKtest0123456789abcd", "SECRETtest0123456789abcdef")
    finally:
        await server.close()


# ----------------------------------------------------------------------------- lacunas achadas por mutacao


async def test_admin_cannot_demote_or_disable_self_even_when_other_admins_exist(
    harness: Harness, admin: Session
) -> None:
    await harness.create_user("admin")  # garante que "restar um admin" NAO e o motivo da recusa
    me = (await admin.get("/auth/me")).json()

    demote = await admin.send("PATCH", f"/users/{me['id']}", json={"role": "reader"})
    disable = await admin.send("PATCH", f"/users/{me['id']}", json={"disabled": True})

    assert demote.status_code == 409 and disable.status_code == 409
    assert (await admin.get("/auth/me")).json()["role"] == "admin"


async def test_session_of_user_disabled_directly_in_database_is_rejected(
    harness: Harness, reader: Session
) -> None:
    async with harness.sessions.begin() as db:
        user = await db.scalar(select(UserRow).where(UserRow.username == reader.username))
        assert user is not None
        user.disabled = True  # sem passar pela API: nenhuma revogacao explicita de sessoes

    assert (await reader.get("/auth/me")).status_code == 401
