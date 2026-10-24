import json
import uuid

import pytest
from sqlalchemy import select, text

from api.audit import AuditService
from api.binance_keys import KeyPermissions
from api.persistence.models import ExchangeCredentialRow
from api.security.crypto import DecryptionError, KeyRing
from api.services.credentials import CredentialService, key_aad, secret_aad
from api.services.maintenance import reencrypt_all
from tests.api_support import GOOD_PASSWORD, MASTER_KEY_V1, MASTER_KEY_V2
from tests.integration.api.conftest import Harness, Session, fake_key

pytestmark = pytest.mark.integration


async def audit_items(admin: Session, **params: str) -> list[dict]:  # type: ignore[type-arg]
    response = await admin.get("/audit", params={"limit": 200, **params})
    assert response.status_code == 200
    return response.json()["items"]


# ---------------------------------------------------------------- usuarios


async def test_admin_manages_users(harness: Harness, admin: Session) -> None:
    name = f"leitor.{uuid.uuid4().hex[:6]}"
    weak = await admin.send(
        "POST", "/users", json={"username": name, "password": "password1234", "role": "reader"}
    )
    assert weak.status_code == 422 and weak.json()["type"].endswith("weak-password")
    assert "password1234" not in weak.text
    bad_name = await admin.send(
        "POST", "/users", json={"username": "Nome Com Espaco!", "password": GOOD_PASSWORD}
    )
    assert bad_name.status_code == 400
    created = await admin.send("POST", "/users", json={"username": name.upper(), "password": GOOD_PASSWORD})
    assert created.status_code == 201
    body = created.json()
    assert body["username"] == name and body["role"] == "reader" and body["totp_enabled"] is False
    assert set(body) == {
        "id", "username", "role", "totp_enabled", "disabled", "created_at", "locked_until",
        "activation_token", "activation_expires_at",
    }  # fmt: skip
    assert body["activation_token"]
    assert "activation_token" not in json.dumps((await admin.get("/users")).json())
    assert (
        await admin.send("POST", "/users", json={"username": name, "password": GOOD_PASSWORD})
    ).status_code == 409

    promoted = await admin.send("PATCH", f"/users/{body['id']}", json={"role": "admin"})
    assert promoted.status_code == 200 and promoted.json()["role"] == "admin"
    assert (await admin.send("PATCH", f"/users/{body['id']}", json={})).status_code == 422
    assert (await admin.send("PATCH", f"/users/{uuid.uuid4()}", json={"disabled": True})).status_code == 404

    listing = (await admin.get("/users")).json()
    assert name in {u["username"] for u in listing}
    assert "password_hash" not in json.dumps(listing) and "totp_secret" not in json.dumps(listing)


async def test_last_admin_and_self_protection(harness: Harness) -> None:
    async with harness.sessions.begin() as db:
        await db.execute(text("UPDATE users SET disabled = true WHERE role = 'admin'"))
    only = await harness.login(await harness.create_user("admin"))
    me = (await only.get("/auth/me")).json()
    demote = await only.send("PATCH", f"/users/{me['id']}", json={"role": "reader"})
    assert demote.status_code == 409
    disable = await only.send("PATCH", f"/users/{me['id']}", json={"disabled": True})
    assert disable.status_code == 409
    reset_self = await only.send("POST", f"/users/{me['id']}/reset-two-factor")
    assert reset_self.status_code == 409
    await only.aclose()


async def test_reset_two_factor_forces_new_enrollment(harness: Harness, admin: Session) -> None:
    name = await harness.create_user("reader")
    member = await harness.login(name)
    user_id = next(u["id"] for u in (await admin.get("/users")).json() if u["username"] == name)
    reset = await admin.send("POST", f"/users/{user_id}/reset-two-factor")
    assert reset.status_code == 200 and reset.json()["activation_token"]
    harness.activation_tokens[name] = reset.json()["activation_token"]
    assert (await member.get("/auth/me")).status_code == 401
    client = harness.client()
    again = await client.post("/auth/login", json={"username": name, "password": GOOD_PASSWORD})
    assert again.json()["stage"] == "enrollment"
    actions = {e["action"] for e in await audit_items(admin, actor=admin.username)}
    assert "user.two_factor_reset" in actions
    await member.aclose()
    await client.aclose()


async def test_totp_secret_is_encrypted_at_rest(harness: Harness) -> None:
    name = await harness.create_user("reader")
    session = await harness.login(name)
    async with harness.sessions() as db:
        stored = await db.scalar(text("SELECT totp_secret FROM users WHERE username = :u"), {"u": name})
    secret = harness.totp_secrets[name]
    assert stored and stored.startswith("enc:v1:") and secret not in stored
    await session.aclose()


# ---------------------------------------------------------------- chaves


async def test_credential_lifecycle_never_returns_secrets(harness: Harness, admin: Session) -> None:
    key, secret = fake_key()
    created = await admin.send(
        "POST", "/credentials", json={"label": "Conta principal", "api_key": key, "api_secret": secret}
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["api_key_last4"] == key[-4:] and body["usable"] is True
    assert body["permissions"]["can_withdraw"] is False
    for text_ in (created.text, (await admin.get("/credentials")).text):
        assert key not in text_ and secret not in text_

    async with harness.sessions() as db:
        row = (
            await db.scalars(
                select(ExchangeCredentialRow).where(ExchangeCredentialRow.id == uuid.UUID(body["id"]))
            )
        ).one()
        raw = json.dumps(
            list((await db.execute(text("SELECT * FROM exchange_credentials"))).mappings().all()), default=str
        )
    assert key not in raw and secret not in raw
    assert row.api_key_enc.startswith("enc:v1:") and row.api_secret_enc.startswith("enc:v1:")

    service = CredentialService(harness.container, AuditService(harness.sessions))
    plain_key, plain_secret = await service.reveal(row.id)
    assert (plain_key.get_secret_value(), plain_secret.get_secret_value()) == (key, secret)
    assert "last_used_at" in (await admin.get("/credentials")).json()[0]
    assert key not in repr(plain_key) and secret not in repr(plain_secret)

    new_key, new_secret = fake_key("AKnew")
    rotated = await admin.send(
        "PUT", f"/credentials/{body['id']}", json={"api_key": new_key, "api_secret": new_secret}
    )
    assert rotated.status_code == 200 and rotated.json()["api_key_last4"] == new_key[-4:]
    assert new_key not in rotated.text and new_secret not in rotated.text
    assert [s.get_secret_value() for s in await service.reveal(row.id)] == [new_key, new_secret]

    verified = await admin.send("POST", f"/credentials/{body['id']}/verify")
    assert verified.status_code == 200 and verified.json()["last_verified_at"]

    assert (await admin.send("DELETE", f"/credentials/{body['id']}")).status_code == 204
    assert (await admin.send("DELETE", f"/credentials/{body['id']}")).status_code == 404
    assert all(c["id"] != body["id"] for c in (await admin.get("/credentials")).json())

    dump = json.dumps(await audit_items(admin))
    for leaked in (key, secret, new_key, new_secret):
        assert leaked not in dump
    actions = {e["action"] for e in await audit_items(admin, action="credential.")}
    assert {
        "credential.created",
        "credential.rotated",
        "credential.deleted",
        "credential.verified",
    } <= actions


async def test_withdraw_permission_is_rejected_and_nothing_is_stored(
    harness: Harness, admin: Session
) -> None:
    harness.verifier.permissions = KeyPermissions(
        can_read=True, can_trade=True, can_withdraw=True, ip_restricted=True
    )
    key, secret = fake_key()
    label = f"Com saque {uuid.uuid4().hex[:4]}"
    response = await admin.send(
        "POST", "/credentials", json={"label": label, "api_key": key, "api_secret": secret}
    )
    assert response.status_code == 422
    assert "sacar dinheiro" in response.json()["detail"]
    assert key not in response.text and secret not in response.text
    assert all(c["label"] != label for c in (await admin.get("/credentials")).json())
    rejected = await audit_items(admin, action="credential.rejected")
    assert rejected and rejected[0]["result"] == "denied"
    harness.verifier.permissions = KeyPermissions(
        can_read=True, can_trade=True, can_withdraw=False, ip_restricted=True
    )


async def test_ip_restriction_warning_and_binance_errors(harness: Harness, admin: Session) -> None:
    harness.verifier.permissions = KeyPermissions(
        can_read=True, can_trade=True, can_withdraw=False, ip_restricted=False
    )
    key, secret = fake_key()
    created = await admin.send(
        "POST",
        "/credentials",
        json={"label": f"Sem IP {uuid.uuid4().hex[:4]}", "api_key": key, "api_secret": secret},
    )
    assert created.status_code == 201 and any("IP" in w for w in created.json()["warnings"])
    harness.verifier.error = "A Binance recusou a chave."
    refused = await admin.send(
        "POST", "/credentials", json={"label": "x", "api_key": key, "api_secret": secret}
    )
    assert refused.status_code == 422 and key not in refused.text
    harness.verifier.error = None
    harness.verifier.permissions = KeyPermissions(can_read=False, can_trade=False, can_withdraw=False)
    no_read = await admin.send(
        "POST", "/credentials", json={"label": "y", "api_key": key, "api_secret": secret}
    )
    assert no_read.status_code == 422
    harness.verifier.permissions = KeyPermissions(
        can_read=True, can_trade=True, can_withdraw=False, ip_restricted=True
    )


async def test_duplicate_label_and_validation_do_not_echo_secrets(harness: Harness, admin: Session) -> None:
    key, secret = fake_key()
    label = f"Dup {uuid.uuid4().hex[:4]}"
    assert (
        await admin.send("POST", "/credentials", json={"label": label, "api_key": key, "api_secret": secret})
    ).status_code == 201
    dup = await admin.send(
        "POST", "/credentials", json={"label": label, "api_key": key, "api_secret": secret}
    )
    assert dup.status_code == 409 and key not in dup.text and secret not in dup.text
    short = await admin.send(
        "POST", "/credentials", json={"label": "z", "api_key": key, "api_secret": "curto-demais"}
    )
    assert short.status_code == 422 and "curto-demais" not in short.text and key not in short.text


async def test_credential_writes_need_fresh_authentication(harness: Harness, admin: Session) -> None:
    harness.clock.advance(minutes=10)
    key, secret = fake_key()
    stale = await admin.send(
        "POST", "/credentials", json={"label": "Velha", "api_key": key, "api_secret": secret}
    )
    assert stale.status_code == 403 and stale.json()["type"].endswith("fresh-auth-required")
    assert (await admin.get("/credentials")).status_code == 200  # leitura nao exige reautenticacao
    assert (
        await admin.send("POST", "/auth/reauth", json={"code": harness.code_for(admin.username, step=1)})
    ).status_code == 200
    ok = await admin.send(
        "POST",
        "/credentials",
        json={"label": f"Nova {uuid.uuid4().hex[:4]}", "api_key": key, "api_secret": secret},
    )
    assert ok.status_code == 201


async def test_aad_binds_ciphertext_to_its_credential(harness: Harness, admin: Session) -> None:
    created = []
    for index in range(2):
        key, secret = fake_key()
        response = await admin.send(
            "POST",
            "/credentials",
            json={"label": f"AAD {index} {uuid.uuid4().hex[:4]}", "api_key": key, "api_secret": secret},
        )
        created.append(uuid.UUID(response.json()["id"]))
    async with harness.sessions.begin() as db:
        first = await db.get(ExchangeCredentialRow, created[0])
        second = await db.get(ExchangeCredentialRow, created[1])
        assert first and second
        first.api_key_enc = second.api_key_enc
    service = CredentialService(harness.container, AuditService(harness.sessions))
    with pytest.raises(DecryptionError):
        await service.reveal(created[0])
    harness.container.keyring = KeyRing.parse(f"1:{MASTER_KEY_V1}\n2:{MASTER_KEY_V2}")
    report = await reencrypt_all(harness.container, AuditService(harness.sessions), actor="cli:test")
    harness.container.keyring = KeyRing.parse(f"1:{MASTER_KEY_V1}")
    assert f"credential:{created[0]}" in report.failed, "dado adulterado e reportado, nao aborta a rotacao"
    async with harness.sessions.begin() as db:
        await db.delete(await db.get(ExchangeCredentialRow, created[0]))
    ring = harness.container.keyring
    with pytest.raises(DecryptionError):
        ring.decrypt(second.api_secret_enc, aad=key_aad(created[1]))
    assert ring.decrypt(second.api_secret_enc, aad=secret_aad(created[1]))


async def test_master_key_rotation_reencrypts_everything(harness: Harness, admin: Session) -> None:
    key, secret = fake_key()
    response = await admin.send(
        "POST",
        "/credentials",
        json={"label": f"Rot {uuid.uuid4().hex[:4]}", "api_key": key, "api_secret": secret},
    )
    credential_id = uuid.UUID(response.json()["id"])
    harness.container.keyring = KeyRing.parse(f"1:{MASTER_KEY_V1}\n2:{MASTER_KEY_V2}")
    report = await reencrypt_all(harness.container, AuditService(harness.sessions), actor="cli:test")
    assert report.credentials >= 1 and report.totp_secrets >= 1
    assert all(not f.endswith(str(credential_id)) for f in report.failed)
    async with harness.sessions() as db:
        row = await db.get(ExchangeCredentialRow, credential_id)
        assert row and row.api_key_enc.startswith("enc:v2:") and row.api_secret_enc.startswith("enc:v2:")
    only_new = KeyRing.parse(f"2:{MASTER_KEY_V2}")
    assert only_new.decrypt(row.api_key_enc, aad=key_aad(credential_id)) == key
    again = await reencrypt_all(harness.container, AuditService(harness.sessions), actor="cli:test")
    assert again.credentials == 0 and again.totp_secrets == 0
    # o login continua funcionando com o segredo TOTP recifrado
    harness.clock.advance(seconds=30)
    relogin = await harness.login(admin.username)
    assert (await relogin.get("/auth/me")).status_code == 200
    await relogin.aclose()
    harness.container.keyring = KeyRing.parse(f"1:{MASTER_KEY_V1}")


# ---------------------------------------------------------------- auditoria


async def test_audit_records_login_events_with_context_and_no_secrets(
    harness: Harness, admin: Session
) -> None:
    name = await harness.create_user("reader")
    client = harness.client()
    await client.post(
        "/auth/login",
        json={"username": name, "password": "senha errada 123456"},
        headers={"X-Request-ID": "req-audit-0001"},
    )
    await client.aclose()
    session = await harness.login(name)
    await session.send("POST", "/auth/logout")
    await session.aclose()

    entries = await audit_items(admin, actor="anonymous")
    failed = next(
        e
        for e in entries
        if e["action"] == "auth.login.failed" and e["details"].get("attempted_user") == name
    )
    assert failed["result"] == "failure" and failed["ip"] == "203.0.113.9"
    assert failed["user_agent"] == "pytest-client" and failed["request_id"] == "req-audit-0001"
    mine = {e["action"] for e in await audit_items(admin, actor=name)}
    assert {"auth.login.password_ok", "auth.2fa.enrolled", "auth.login.succeeded", "auth.logout"} <= mine
    everything = json.dumps(await audit_items(admin))
    assert GOOD_PASSWORD not in everything and "senha errada 123456" not in everything
    assert harness.totp_secrets[name] not in everything


async def test_audit_pagination_is_stable(harness: Harness, admin: Session) -> None:
    first = (await admin.get("/audit", params={"limit": 5})).json()
    assert len(first["items"]) == 5 and first["next_cursor"] == first["items"][-1]["id"]
    second = (await admin.get("/audit", params={"limit": 5, "cursor": first["next_cursor"]})).json()
    ids = [e["id"] for e in first["items"] + second["items"]]
    assert ids == sorted(ids, reverse=True) and len(set(ids)) == 10
    assert (await admin.get("/audit", params={"limit": 0})).status_code == 422
    assert (await admin.get("/audit", params={"limit": 500})).status_code == 422


async def test_audit_log_stays_append_only(harness: Harness, admin: Session) -> None:
    async with harness.sessions() as db:
        with pytest.raises(Exception, match="append-only"):
            await db.execute(
                text("UPDATE audit_log SET actor = 'x' WHERE id = (SELECT max(id) FROM audit_log)")
            )
