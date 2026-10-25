"""Matriz rota x papel com sessoes reais: anonimo, leitor e admin em CADA rota documentada."""

import json
import uuid

import pytest

from api.export_openapi import build_schema_text
from tests.integration.api.conftest import Harness, Session

pytestmark = pytest.mark.integration

PUBLIC = {("GET", "/health"), ("GET", "/ready"), ("POST", "/auth/login")}
PENDING_ONLY = {
    ("POST", "/auth/2fa/verify"),
    ("POST", "/auth/2fa/enroll"),
    ("POST", "/auth/2fa/enroll/confirm"),
}
ADMIN_ONLY = {
    ("GET", "/users"),
    ("POST", "/users"),
    ("PATCH", "/users/{user_id}"),
    ("POST", "/users/{user_id}/reset-two-factor"),
    ("POST", "/users/{user_id}/unlock"),
    ("GET", "/audit"),
    ("GET", "/credentials"),
    ("POST", "/credentials"),
    ("PUT", "/credentials/{credential_id}"),
    ("DELETE", "/credentials/{credential_id}"),
    ("POST", "/credentials/{credential_id}/verify"),
    ("POST", "/backtests"),
    ("POST", "/backtests/{job_id}/cancel"),
    ("POST", "/control/pause"),
    ("POST", "/control/resume"),
    ("POST", "/control/stop"),
    ("POST", "/control/emergency-stop"),
    ("POST", "/control/reactivate"),
    ("POST", "/control/power"),
    ("POST", "/live/activate"),
    ("POST", "/live/authorization/revoke"),
    ("POST", "/ai/lessons/run"),
    ("POST", "/ai/lessons/{lesson_id}/review"),
    ("POST", "/settings/validate"),
    ("PUT", "/settings/{section}"),
    ("POST", "/onboarding/start-paper"),
}


def operations() -> list[tuple[str, str]]:
    schema = json.loads(build_schema_text())
    return sorted((m.upper(), p) for p, item in schema["paths"].items() for m in item)


def concrete(path: str) -> str:
    return (
        path.replace("{user_id}", str(uuid.uuid4()))
        .replace("{credential_id}", str(uuid.uuid4()))
        .replace("{session_id}", str(uuid.uuid4()))
        .replace("{job_id}", uuid.uuid4().hex)
        .replace("{command_id}", uuid.uuid4().hex)
        .replace("{section}", "risk")
        .replace("{lesson_id}", uuid.uuid4().hex)
    )


async def call(session: Session, method: str, path: str):  # type: ignore[no-untyped-def]
    return await session.client.request(method, concrete(path), headers=session.headers, json={})


@pytest.mark.parametrize(("method", "path"), [op for op in operations() if op not in PUBLIC])
async def test_anonymous_is_always_unauthorized(harness: Harness, method: str, path: str) -> None:
    client = harness.client()
    response = await client.request(method, concrete(path), json={})
    assert response.status_code == 401, f"{method} {path} acessivel sem sessao"
    await client.aclose()


@pytest.mark.parametrize(("method", "path"), [op for op in operations() if op in ADMIN_ONLY])
async def test_reader_is_forbidden_on_admin_routes_and_admin_is_not(
    harness: Harness, admin: Session, reader: Session, method: str, path: str
) -> None:
    denied = await call(reader, method, path)
    assert denied.status_code == 403, f"{method} {path}: leitor recebeu {denied.status_code}"
    assert denied.headers["content-type"].startswith("application/problem+json")
    allowed = await call(admin, method, path)
    assert allowed.status_code not in {401, 403}, f"{method} {path}: admin recebeu {allowed.status_code}"


@pytest.mark.parametrize(
    ("method", "path"), [op for op in operations() if op not in PUBLIC | ADMIN_ONLY | PENDING_ONLY]
)
async def test_every_other_route_is_open_to_reader_and_admin(
    harness: Harness, admin: Session, reader: Session, method: str, path: str
) -> None:
    if (method, path) == ("POST", "/auth/logout"):
        pytest.skip("encerra a sessao; coberto em test_auth_flow")
    for session in (reader, admin):
        response = await call(session, method, path)
        assert response.status_code not in {401, 403} or response.json()["type"].endswith(
            "fresh-auth-required"
        )


@pytest.mark.parametrize(("method", "path"), [op for op in operations() if op in PENDING_ONLY])
async def test_full_session_is_not_accepted_on_second_step_routes(
    harness: Harness, admin: Session, method: str, path: str
) -> None:
    response = await call(admin, method, path)
    assert response.status_code == 401


async def test_reader_denial_is_audited(harness: Harness, admin: Session, reader: Session) -> None:
    assert (
        await reader.send("POST", "/users", json={"username": "x.y.z", "password": "a" * 20})
    ).status_code == 403
    entries = (await admin.get("/audit", params={"action": "authz."})).json()["items"]
    assert any(e["actor"] == reader.username and e["result"] == "denied" for e in entries)


async def test_cross_origin_post_with_valid_session_is_blocked(harness: Harness, admin: Session) -> None:
    response = await admin.client.post(
        "/auth/logout", headers={**admin.headers, "Origin": "https://evil.example"}
    )
    assert response.status_code == 403
    assert (await admin.get("/auth/me")).status_code == 200
