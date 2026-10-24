import json

import pytest
from sqlalchemy import select, text

from api.persistence.models import RecoveryCodeRow, SessionRow, UserRow
from api.security.throttle import FailureThrottle
from api.security.tokens import hash_token
from tests.api_support import GOOD_PASSWORD, OTHER_PASSWORD
from tests.integration.api.conftest import Harness

pytestmark = pytest.mark.integration


async def _cookie_names(response) -> list[str]:  # type: ignore[no-untyped-def]
    return [c.split("=", 1)[0] for c in response.headers.get_list("set-cookie")]


async def test_bootstrap_login_enroll_session_csrf_logout(harness: Harness) -> None:
    username = await harness.create_user("admin")
    client = harness.client()

    login = await client.post("/auth/login", json={"username": username, "password": GOOD_PASSWORD})
    assert login.status_code == 200 and login.json()["stage"] == "enrollment"
    set_cookies = login.headers.get_list("set-cookie")
    session_cookie = next(c for c in set_cookies if c.startswith("ci_session="))
    assert "HttpOnly" in session_cookie and "SameSite=strict" in session_cookie.replace("Strict", "strict")
    csrf_cookie = next(c for c in set_cookies if c.startswith("ci_csrf="))
    assert "HttpOnly" not in csrf_cookie
    csrf = login.json()["csrf_token"]

    # login pendente nao vale como sessao
    assert (await client.get("/auth/me")).status_code == 401
    assert (await client.get("/users")).status_code == 401

    # 2o passo sem CSRF e recusado
    token = {"activation_token": harness.activation_tokens[username]}
    assert (await client.post("/auth/2fa/enroll", json=token)).status_code == 403
    begin = await client.post("/auth/2fa/enroll", json=token, headers={"X-CSRF-Token": csrf})
    assert begin.status_code == 200
    body = begin.json()
    assert body["otpauth_uri"].startswith("otpauth://totp/") and body["qr_svg_data_uri"].startswith(
        "data:image/svg+xml"
    )
    harness.totp_secrets[username] = body["secret"]

    wrong = await client.post(
        "/auth/2fa/enroll/confirm", json={"code": "000000"}, headers={"X-CSRF-Token": csrf}
    )
    assert wrong.status_code == 401
    harness.clock.advance(seconds=30)
    done = await client.post(
        "/auth/2fa/enroll/confirm", json={"code": harness.code_for(username)}, headers={"X-CSRF-Token": csrf}
    )
    assert done.status_code == 200
    codes = done.json()["recovery_codes"]
    assert len(codes) == 10
    csrf = done.json()["csrf_token"]
    assert csrf != login.json()["csrf_token"], "a sessao e rotacionada ao concluir o login"

    me = await client.get("/auth/me")
    assert me.status_code == 200 and me.json()["username"] == username and me.json()["role"] == "admin"
    assert me.json()["csrf_token"] == csrf

    # a sessao pendente antiga morreu (fixacao de sessao)
    async with harness.sessions() as db:
        rows = (await db.scalars(select(SessionRow).join(UserRow).where(UserRow.username == username))).all()
    assert sorted(r.revoked_reason or "ativa" for r in rows) == ["ativa", "rotated"]

    # CSRF obrigatorio em metodo mutavel
    assert (await client.post("/auth/logout")).status_code == 403
    assert (await client.post("/auth/logout", headers={"X-CSRF-Token": "forjado"})).status_code == 403
    # cookie plantado pelo atacante com header igual, mas fora da sessao, tambem falha
    client.cookies.set("ci_csrf", "forjado")
    assert (await client.post("/auth/logout", headers={"X-CSRF-Token": "forjado"})).status_code == 403
    client.cookies.set("ci_csrf", csrf)

    out = await client.post("/auth/logout", headers={"X-CSRF-Token": csrf})
    assert out.status_code == 204
    assert (await client.get("/auth/me")).status_code == 401


async def test_second_login_needs_totp_and_replay_is_rejected(harness: Harness) -> None:
    username = await harness.create_user("admin")
    first = await harness.login(username)
    await first.aclose()

    client = harness.client()
    step1 = await client.post("/auth/login", json={"username": username, "password": GOOD_PASSWORD})
    assert step1.json()["stage"] == "totp"
    csrf = step1.json()["csrf_token"]
    # codigo do passo de enrolamento (ja consumido) nao serve de novo
    replay = await client.post(
        "/auth/2fa/verify", json={"code": harness.code_for(username)}, headers={"X-CSRF-Token": csrf}
    )
    assert replay.status_code == 401
    harness.clock.advance(seconds=30)
    ok = await client.post(
        "/auth/2fa/verify", json={"code": harness.code_for(username)}, headers={"X-CSRF-Token": csrf}
    )
    assert ok.status_code == 200
    # o mesmo codigo nao pode abrir outra sessao
    other = harness.client()
    again = await other.post("/auth/login", json={"username": username, "password": GOOD_PASSWORD})
    reuse = await other.post(
        "/auth/2fa/verify",
        json={"code": harness.code_for(username)},
        headers={"X-CSRF-Token": again.json()["csrf_token"]},
    )
    assert reuse.status_code == 401
    await client.aclose()
    await other.aclose()


async def test_recovery_code_is_single_use(harness: Harness) -> None:
    username = await harness.create_user("reader")
    first = await harness.login(username)
    code = first.recovery_codes[0]
    await first.aclose()

    async def attempt(recovery: str) -> int:
        client = harness.client()
        step = await client.post("/auth/login", json={"username": username, "password": GOOD_PASSWORD})
        response = await client.post(
            "/auth/2fa/verify",
            json={"recovery_code": recovery},
            headers={"X-CSRF-Token": step.json()["csrf_token"]},
        )
        await client.aclose()
        return response.status_code

    assert await attempt(code.lower()) == 200
    assert await attempt(code) == 401
    async with harness.sessions() as db:
        used = (await db.scalars(select(RecoveryCodeRow).where(RecoveryCodeRow.used_at.is_not(None)))).all()
        stored = (await db.scalars(select(RecoveryCodeRow))).all()
    assert used and all(code.replace("-", "") not in r.code_hash for r in stored)


async def test_wrong_credentials_are_indistinguishable(harness: Harness) -> None:
    username = await harness.create_user("admin")
    client = harness.client()
    wrong_password = await client.post(
        "/auth/login", json={"username": username, "password": "senha errada 123456"}
    )
    unknown = await client.post(
        "/auth/login", json={"username": "ninguem.existe", "password": "senha errada 123456"}
    )
    disabled_name = await harness.create_user("reader")
    async with harness.sessions.begin() as db:
        await db.execute(text("UPDATE users SET disabled = true WHERE username = :u"), {"u": disabled_name})
    disabled = await client.post("/auth/login", json={"username": disabled_name, "password": GOOD_PASSWORD})
    for response in (wrong_password, unknown, disabled):
        assert response.status_code == 401

    def shape(r):  # type: ignore[no-untyped-def]
        body = r.json()
        return {k: v for k, v in body.items() if k not in {"request_id"}}

    assert shape(wrong_password) == shape(unknown) == shape(disabled)


async def test_progressive_lockout_is_uniform_for_unknown_users(harness: Harness) -> None:
    known = await harness.create_user("reader")

    async def hammer(name: str) -> list[int]:
        client = harness.client()
        codes = []
        for _ in range(7):
            response = await client.post(
                "/auth/login", json={"username": name, "password": "senha errada 123456"}
            )
            codes.append(response.status_code)
        retry = response.headers.get("retry-after")
        await client.aclose()
        assert retry is not None and int(retry) > 0
        return codes

    assert await hammer(known) == [401] * 5 + [429] * 2
    assert await hammer("fantasma.inexistente") == [401] * 5 + [429] * 2
    # mesmo com a senha certa, o usuario bloqueado nao entra
    client = harness.client()
    blocked = await client.post("/auth/login", json={"username": known, "password": GOOD_PASSWORD})
    assert blocked.status_code == 429
    async with harness.sessions() as db:
        user = await db.scalar(select(UserRow).where(UserRow.username == known))
    assert user is not None and user.locked_until is None, "senha errada nunca trava a conta no banco"
    await client.aclose()


async def test_password_lock_is_per_user_and_ip_and_clears_with_the_throttle(harness: Harness) -> None:
    username = await harness.create_user("reader")
    client = harness.client()
    for _ in range(5):
        await client.post("/auth/login", json={"username": username, "password": "senha errada 123456"})
    blocked = await client.post("/auth/login", json={"username": username, "password": GOOD_PASSWORD})
    assert blocked.status_code == 429
    harness.container.user_throttle = FailureThrottle(threshold=5)
    harness.container.ip_throttle = FailureThrottle(threshold=20)
    assert (
        await client.post("/auth/login", json={"username": username, "password": GOOD_PASSWORD})
    ).status_code == 200
    await client.aclose()


async def test_totp_failures_lock_and_kill_pending_session(harness: Harness) -> None:
    username = await harness.create_user("reader")
    await (await harness.login(username)).aclose()  # enrola o 2FA
    client = harness.client()
    step = await client.post("/auth/login", json={"username": username, "password": GOOD_PASSWORD})
    csrf = step.json()["csrf_token"]
    statuses = []
    for _ in range(5):
        statuses.append(
            (
                await client.post("/auth/2fa/verify", json={"code": "000000"}, headers={"X-CSRF-Token": csrf})
            ).status_code
        )
    assert statuses == [401] * 5
    harness.clock.advance(seconds=1)
    after = await client.post(
        "/auth/2fa/verify", json={"code": harness.code_for(username, step=2)}, headers={"X-CSRF-Token": csrf}
    )
    assert after.status_code == 401, "a sessao pendente foi revogada apos 5 falhas"
    relogin = await client.post("/auth/login", json={"username": username, "password": GOOD_PASSWORD})
    assert relogin.status_code == 429, "e o usuario ficou bloqueado"
    async with harness.sessions() as db:
        user = await db.scalar(select(UserRow).where(UserRow.username == username))
        reasons = (
            await db.scalars(select(SessionRow.revoked_reason).where(SessionRow.user_id == user.id))
        ).all()  # type: ignore[union-attr]
    assert user is not None and user.failed_attempts >= 5 and "too_many_failures" in reasons
    await client.aclose()


async def test_session_idle_and_absolute_expiry_and_revocation(harness: Harness) -> None:
    username = await harness.create_user("admin")
    session = await harness.login(username)
    assert (await session.get("/auth/me")).status_code == 200

    harness.clock.advance(minutes=20)
    assert (await session.get("/auth/me")).status_code == 200  # atividade renova a janela de inatividade
    harness.clock.advance(minutes=20)
    assert (await session.get("/auth/me")).status_code == 200
    harness.clock.advance(minutes=31)
    assert (await session.get("/auth/me")).status_code == 401, "inatividade"

    again = await harness.login(username)
    for _ in range(26):
        harness.clock.advance(minutes=29)
        response = await again.get("/auth/me")
        if response.status_code == 401:
            break
    assert response.status_code == 401, "expiracao absoluta de 12 h mesmo com atividade"
    await session.aclose()
    await again.aclose()


async def test_revoking_another_session_and_logout_everywhere(harness: Harness) -> None:
    username = await harness.create_user("reader")
    one = await harness.login(username)
    two = await harness.login(username)
    listing = (await one.get("/sessions")).json()
    assert len(listing) == 2 and sum(s["current"] for s in listing) == 1
    other_id = next(s["id"] for s in listing if not s["current"])
    assert (await one.send("DELETE", f"/sessions/{other_id}")).status_code == 204
    assert (await two.get("/auth/me")).status_code == 401
    assert (await one.send("DELETE", f"/sessions/{other_id}")).status_code == 204  # idempotente
    stranger = await harness.login(await harness.create_user("reader"))
    assert (await stranger.send("DELETE", f"/sessions/{listing[0]['id']}")).status_code == 404
    third = await harness.login(username)
    assert (await third.send("DELETE", "/sessions")).status_code == 204
    assert (await one.get("/auth/me")).status_code == 401
    assert (await third.get("/auth/me")).status_code == 200
    for s in (one, two, stranger, third):
        await s.aclose()


async def test_password_change_revokes_other_sessions_and_checks_current(harness: Harness) -> None:
    username = await harness.create_user("reader")
    keep = await harness.login(username)
    other = await harness.login(username)
    bad = await keep.send(
        "POST",
        "/auth/password",
        json={"current_password": "errada errada 123", "new_password": OTHER_PASSWORD},
    )
    assert bad.status_code == 401
    weak = await keep.send(
        "POST", "/auth/password", json={"current_password": GOOD_PASSWORD, "new_password": "password1234"}
    )
    assert weak.status_code == 422 and "password1234" not in weak.text
    same = await keep.send(
        "POST", "/auth/password", json={"current_password": GOOD_PASSWORD, "new_password": GOOD_PASSWORD}
    )
    assert same.status_code == 409
    ok = await keep.send(
        "POST", "/auth/password", json={"current_password": GOOD_PASSWORD, "new_password": OTHER_PASSWORD}
    )
    assert ok.status_code == 204
    assert (await keep.get("/auth/me")).status_code == 200
    assert (await other.get("/auth/me")).status_code == 401
    client = harness.client()
    assert (
        await client.post("/auth/login", json={"username": username, "password": GOOD_PASSWORD})
    ).status_code == 401
    assert (
        await client.post("/auth/login", json={"username": username, "password": OTHER_PASSWORD})
    ).status_code == 200
    await keep.aclose()
    await other.aclose()
    await client.aclose()


async def test_fresh_auth_window_and_reauth(harness: Harness) -> None:
    session = await harness.login(await harness.create_user("admin"))
    assert (await session.send("POST", "/auth/2fa/recovery-codes")).status_code == 200
    harness.clock.advance(minutes=6)
    stale = await session.send("POST", "/auth/2fa/recovery-codes")
    assert stale.status_code == 403 and stale.json()["type"].endswith("fresh-auth-required")
    me = (await session.get("/auth/me")).json()
    assert me["fresh_auth_until"] is None
    bad = await session.send("POST", "/auth/reauth", json={"code": "000000"})
    assert bad.status_code == 401
    ok = await session.send("POST", "/auth/reauth", json={"code": harness.code_for(session.username, step=1)})
    assert ok.status_code == 200 and ok.json()["fresh_auth_until"]
    fresh = await session.send("POST", "/auth/2fa/recovery-codes")
    assert fresh.status_code == 200 and len(fresh.json()["recovery_codes"]) == 10
    await session.aclose()


async def test_session_token_is_stored_hashed(harness: Harness) -> None:
    session = await harness.login(await harness.create_user("reader"))
    token = session.client.cookies.get("ci_session")
    assert token
    async with harness.sessions() as db:
        hashes = set((await db.scalars(select(SessionRow.token_hash))).all())
        dump = json.dumps(
            list((await db.execute(text("SELECT * FROM user_sessions"))).mappings().all()), default=str
        )
    assert hash_token(token) in hashes and token not in dump
    await session.aclose()


async def test_disabled_user_loses_active_sessions(harness: Harness) -> None:
    admin = await harness.login(await harness.create_user("admin"))
    victim_name = await harness.create_user("reader")
    victim = await harness.login(victim_name)
    users = (await admin.get("/users")).json()
    victim_id = next(u["id"] for u in users if u["username"] == victim_name)
    assert (await victim.get("/auth/me")).status_code == 200
    assert (await admin.send("PATCH", f"/users/{victim_id}", json={"disabled": True})).status_code == 200
    assert (await victim.get("/auth/me")).status_code == 401
    client = harness.client()
    assert (
        await client.post("/auth/login", json={"username": victim_name, "password": GOOD_PASSWORD})
    ).status_code == 401
    for s in (admin, victim):
        await s.aclose()
    await client.aclose()
