"""Endurecimento da fundacao da API (fase de correcao da revisao de seguranca)."""

import asyncio
import time
import uuid

import pytest
from sqlalchemy import select, text

from api.audit import AuditService
from api.persistence.models import ExchangeCredentialRow, UserRow
from api.services.maintenance import reencrypt_all
from engine.infrastructure.persistence.models import AuditLogRow
from tests.api_support import GOOD_PASSWORD
from tests.integration.api.conftest import Harness, Session, fake_key

pytestmark = pytest.mark.integration


async def _audit(harness: Harness, action: str | None = None) -> list[AuditLogRow]:
    async with harness.sessions() as db:
        query = select(AuditLogRow).order_by(AuditLogRow.id)
        if action:
            query = query.where(AuditLogRow.action == action)
        return list((await db.scalars(query)).all())


async def _login_step(harness: Harness, username: str, ip: str = "203.0.113.9") -> tuple[object, str]:
    client = harness.client(ip=ip)
    first = await client.post("/auth/login", json={"username": username, "password": GOOD_PASSWORD})
    assert first.status_code == 200, first.text
    return client, first.json()["csrf_token"]


# ---------------------------------------------------------------- token de ativacao


async def test_enrollment_requires_the_activation_token(harness: Harness) -> None:
    username = await harness.create_user("reader")
    client, csrf = await _login_step(harness, username)
    headers = {"X-CSRF-Token": csrf}

    missing = await client.post("/auth/2fa/enroll", headers=headers)  # type: ignore[attr-defined]
    assert missing.status_code == 422
    wrong = await client.post(  # type: ignore[attr-defined]
        "/auth/2fa/enroll", json={"activation_token": "x" * 32}, headers=headers
    )
    assert wrong.status_code == 401
    async with harness.sessions() as db:
        user = await db.scalar(select(UserRow).where(UserRow.username == username))
    assert user is not None and user.totp_secret is None, "sem o token nenhum segredo TOTP e gerado"
    failed = [r for r in await _audit(harness, "auth.activation.failed") if r.actor == username]
    assert failed
    token = harness.activation_tokens[username]
    assert all(token not in str(r.payload) for r in await _audit(harness))


async def test_activation_token_is_hashed_single_use_and_audited(harness: Harness) -> None:
    username = await harness.create_user("reader")
    token = harness.activation_tokens[username]
    async with harness.sessions() as db:
        stored = await db.scalar(select(UserRow.activation_token_hash).where(UserRow.username == username))
    assert stored and token not in stored and len(stored) == 64

    session = await harness.login(username)
    await session.aclose()
    async with harness.sessions() as db:
        user = await db.scalar(select(UserRow).where(UserRow.username == username))
    assert user is not None and user.activation_token_hash is None and user.totp_enabled

    client, csrf = await _login_step(harness, username)
    harness.clock.advance(seconds=30)
    again = await client.post(  # type: ignore[attr-defined]
        "/auth/2fa/enroll", json={"activation_token": token}, headers={"X-CSRF-Token": csrf}
    )
    assert again.status_code == 409, "2FA ja ativo: nao ha novo enrollment"
    actions = [r.action for r in await _audit(harness)]
    assert "user.activation_issued" in actions and "auth.activation.used" in actions
    assert all(token not in str(r.payload) for r in await _audit(harness))


async def test_expired_activation_token_is_refused(harness: Harness) -> None:
    username = await harness.create_user("reader")
    harness.clock.advance(hours=harness.settings.activation_token_hours, seconds=1)
    client, csrf = await _login_step(harness, username)
    refused = await client.post(  # type: ignore[attr-defined]
        "/auth/2fa/enroll",
        json={"activation_token": harness.activation_tokens[username]},
        headers={"X-CSRF-Token": csrf},
    )
    assert refused.status_code == 401


async def test_creating_a_user_returns_the_token_once_and_reset_issues_a_new_one(
    harness: Harness, admin: Session
) -> None:
    created = await admin.send(
        "POST",
        "/users",
        json={"username": f"novo.{uuid.uuid4().hex[:6]}", "password": GOOD_PASSWORD, "role": "reader"},
    )
    assert created.status_code == 201
    body = created.json()
    assert body["activation_token"] and body["activation_expires_at"]
    listed = next(u for u in (await admin.get("/users")).json() if u["id"] == body["id"])
    assert "activation_token" not in listed
    first = body["activation_token"]

    reset = await admin.send("POST", f"/users/{body['id']}/reset-two-factor")
    assert reset.status_code == 200 and reset.json()["activation_token"] != first
    harness.activation_tokens[body["username"]] = first
    client, csrf = await _login_step(harness, body["username"])
    old = await client.post(  # type: ignore[attr-defined]
        "/auth/2fa/enroll", json={"activation_token": first}, headers={"X-CSRF-Token": csrf}
    )
    assert old.status_code == 401, "o token anterior morre quando outro e emitido"


# ---------------------------------------------------------------- admin: desbloqueio e reauth


async def test_admin_unlocks_a_user_with_fresh_auth_and_it_is_audited(
    harness: Harness, admin: Session
) -> None:
    name = await harness.create_user("reader")
    async with harness.sessions.begin() as db:
        await db.execute(
            text(
                "UPDATE users SET failed_attempts = 9, locked_until = now() + interval '1 hour' "
                "WHERE username = :u"
            ),
            {"u": name},
        )
        user_id = await db.scalar(text("SELECT id FROM users WHERE username = :u"), {"u": name})

    harness.clock.advance(minutes=10)
    stale = await admin.send("POST", f"/users/{user_id}/unlock")
    assert stale.status_code == 403

    reauth = await admin.send("POST", "/auth/reauth", json={"code": harness.code_for(admin.username, step=0)})
    assert reauth.status_code == 200, reauth.text
    done = await admin.send("POST", f"/users/{user_id}/unlock")
    assert done.status_code == 200 and done.json()["locked_until"] is None
    assert (await admin.send("POST", f"/users/{uuid.uuid4()}/unlock")).status_code == 404
    async with harness.sessions() as db:
        user = await db.scalar(select(UserRow).where(UserRow.username == name))
    assert user is not None and user.failed_attempts == 0 and user.locked_until is None
    assert [r for r in await _audit(harness, "user.unlocked") if r.actor == admin.username]


async def test_readers_cannot_unlock(harness: Harness, reader: Session) -> None:
    assert (await reader.send("POST", f"/users/{uuid.uuid4()}/unlock")).status_code == 403


# ---------------------------------------------------------------- auditoria de tentativas


async def test_failed_login_audit_never_stores_free_text(harness: Harness) -> None:
    known = await harness.create_user("reader")
    client = harness.client(ip="192.0.2.50")
    typed = "<script>alert('x')</script> senha:hunter2"
    await client.post("/auth/login", json={"username": typed, "password": "errada errada 123!"})
    await client.post("/auth/login", json={"username": "fantasma.xyz", "password": "errada errada 123!"})
    await client.post("/auth/login", json={"username": known, "password": "errada errada 123!"})
    rows = await _audit(harness, "auth.login.failed")
    blob = " ".join(str(r.payload) for r in rows)
    assert "script" not in blob and "hunter2" not in blob and "fantasma" not in blob
    assert known in blob and "attempted_tag" in blob


async def test_rate_limited_responses_are_audited_once_per_window(harness: Harness) -> None:
    name = await harness.create_user("reader")
    client = harness.client(ip="192.0.2.51")
    for _ in range(5):
        await client.post("/auth/login", json={"username": name, "password": "errada errada 123!"})
    codes = [
        (await client.post("/auth/login", json={"username": name, "password": "errada"})).status_code
        for _ in range(12)
    ]
    assert set(codes) == {429}
    events = [r for r in await _audit(harness, "auth.login.rate_limited") if r.payload["ip"] == "192.0.2.51"]
    assert len(events) == 1


async def test_failures_across_many_ips_alert_but_never_hard_lock_the_user(harness: Harness) -> None:
    name = await harness.create_user("reader")
    for i in range(harness.settings.login_alert_failures + 2):
        client = harness.client(ip=f"198.51.100.{i + 1}")
        r = await client.post("/auth/login", json={"username": name, "password": "errada errada 123!"})
        assert r.status_code == 401
    assert await _audit(harness, "auth.login.targeted")
    ok = harness.client(ip="203.0.113.77")
    assert (
        await ok.post("/auth/login", json={"username": name, "password": GOOD_PASSWORD})
    ).status_code == 200


# ---------------------------------------------------------------- inundacao de logins


async def test_login_burst_does_not_starve_authenticated_routes(
    harness: Harness, admin: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = harness.container.passwords.verify

    async def slow_verify(stored_hash: str | None, password: str) -> bool:
        await asyncio.sleep(0.15)
        return await original(stored_hash, password)

    monkeypatch.setattr(harness.container.passwords, "verify", slow_verify)
    gate = harness.container.hash_gate

    async def attempt(i: int) -> int:
        client = harness.client(ip=f"10.9.{i // 200}.{i % 200 + 1}")
        r = await client.post("/auth/login", json={"username": f"x{i}.fantasma", "password": "y" * 14})
        await client.aclose()
        return r.status_code

    burst = asyncio.gather(*(attempt(i) for i in range(60)))
    await asyncio.sleep(0.05)
    started = time.monotonic()
    me = await admin.get("/auth/me")
    elapsed = time.monotonic() - started
    codes = await burst
    assert me.status_code == 200 and elapsed < 2.0, elapsed
    assert gate.peak <= harness.settings.login_max_concurrent_hashes
    assert set(codes) <= {401, 429}


async def test_password_is_verified_outside_any_transaction(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Argon2 nao pode segurar conexao do pool nem lock de linha: durante o verify o usuario esta livre."""
    username = await harness.create_user("reader")
    original = harness.container.passwords.verify
    locked: list[bool] = []

    async def probing_verify(stored_hash: str | None, password: str) -> bool:
        async with harness.sessions.begin() as db:
            row = await db.execute(
                text("SELECT id FROM users WHERE username = :u FOR UPDATE NOWAIT"), {"u": username}
            )
            locked.append(row.first() is not None)
        return await original(stored_hash, password)

    monkeypatch.setattr(harness.container.passwords, "verify", probing_verify)
    client = harness.client(ip="192.0.2.80")
    assert (
        await client.post("/auth/login", json={"username": username, "password": GOOD_PASSWORD})
    ).status_code == 200
    assert locked == [True]


# ---------------------------------------------------------------- manutencao


async def test_reencrypt_waits_for_row_locks_instead_of_overwriting(harness: Harness, admin: Session) -> None:
    key, secret = fake_key()
    created = await admin.send(
        "POST",
        "/credentials",
        json={"label": f"k{uuid.uuid4().hex[:8]}", "api_key": key, "api_secret": secret},
    )
    assert created.status_code == 201
    credential_id = uuid.UUID(created.json()["id"])
    audit = AuditService(harness.sessions)
    async with harness.sessions.begin() as db:
        await db.get(ExchangeCredentialRow, credential_id, with_for_update=True)
        task = asyncio.create_task(reencrypt_all(harness.container, audit, actor="test"))
        await asyncio.sleep(0.5)
        assert not task.done(), "a recifragem tem que esperar o lock da credencial"
    report = await asyncio.wait_for(task, 30)
    assert report.failed == ()


# ---------------------------------------------------------------- CSRF e proxies


async def test_csrf_denial_is_audited_and_sampled(harness: Harness, admin: Session) -> None:
    before = len(await _audit(harness, "authz.denied"))
    for _ in range(5):
        assert (await admin.client.post("/auth/logout")).status_code == 403
    assert len(await _audit(harness, "authz.denied")) == before + 1


async def test_foreign_origin_denial_is_audited(harness: Harness, admin: Session) -> None:
    before = len(await _audit(harness, "authz.denied"))
    response = await admin.client.post(
        "/auth/logout", headers={**admin.headers, "Origin": "https://evil.example"}
    )
    assert response.status_code == 403
    assert len(await _audit(harness, "authz.denied")) == before + 1
