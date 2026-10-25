"""Politica de autorizacao estatica: toda rota declara quem pode chama-la (falha para rota nova esquecida)."""

import json
import re
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import async_sessionmaker

from api.app import create_app
from api.deps import require_admin, require_fresh_admin, require_fresh_auth, require_pending, require_user
from api.export_openapi import OPENAPI_PATH, build_schema_text
from api.routers import PROTECTED_ROUTERS, PUBLIC_ROUTERS
from tests.api_support import make_settings

PUBLIC = {("GET", "/health"), ("GET", "/ready"), ("POST", "/auth/login")}
# Rotas de qualquer usuario autenticado que mutam so o proprio estado (leitor tambem as usa).
SELF_SERVICE = {
    ("POST", "/auth/logout"),
    ("POST", "/auth/reauth"),
    ("POST", "/auth/password"),
    ("POST", "/auth/2fa/recovery-codes"),
    ("DELETE", "/sessions/{session_id}"),
    ("DELETE", "/sessions"),
}
PENDING = {("POST", "/auth/2fa/verify"), ("POST", "/auth/2fa/enroll"), ("POST", "/auth/2fa/enroll/confirm")}
EXPECTED_ADMIN_ROUTES = {
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
    ("POST", "/settings/validate"),
    ("PUT", "/settings/{section}"),
    ("POST", "/onboarding/start-paper"),
    ("POST", "/ai/lessons/run"),
    ("POST", "/ai/lessons/{lesson_id}/review"),
}


@pytest.fixture(scope="module")
def app() -> FastAPI:
    return create_app(make_settings(), sessions=async_sessionmaker())


def _calls(dependant: Dependant) -> Iterator[object]:
    for sub in dependant.dependencies:
        yield sub.call
        yield from _calls(sub)


def _routes(app: FastAPI) -> list[tuple[str, str, set[object]]]:
    """Rotas dos roteadores registrados (o FastAPI 0.15x inclui roteadores de forma preguicosa)."""
    found = []
    for router in [*PUBLIC_ROUTERS, *PROTECTED_ROUTERS]:
        for route in router.routes:
            assert isinstance(route, APIRoute)
            for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
                found.append((method, route.path, set(_calls(route.dependant))))
    return found


def test_no_route_escapes_the_registered_routers(app: FastAPI) -> None:
    declared = {(m, p) for m, p, _ in _routes(app)}
    documented = {(method.upper(), path) for path, item in app.openapi()["paths"].items() for method in item}
    assert documented == declared
    assert len(declared) >= 20


def test_every_route_is_public_or_requires_a_session(app: FastAPI) -> None:
    gates = {require_user, require_pending, require_admin, require_fresh_auth, require_fresh_admin}
    for method, path, calls in _routes(app):
        if (method, path) in PUBLIC:
            assert not calls & gates, f"{method} {path} deveria ser publica"
        else:
            assert calls & gates, f"{method} {path} sem exigencia de sessao"


def test_public_routes_are_exactly_the_allowlist(app: FastAPI) -> None:
    gates = {require_user, require_pending, require_admin, require_fresh_auth, require_fresh_admin}
    open_routes = {(m, p) for m, p, calls in _routes(app) if not calls & gates}
    assert open_routes == PUBLIC


def test_every_mutating_route_requires_admin_unless_self_service(app: FastAPI) -> None:
    """Leitor so le: mutacao exige admin, salvo as rotas de autoatendimento listadas e o 2o passo do login."""
    for method, path, calls in _routes(app):
        if method == "GET" or (method, path) in PUBLIC | SELF_SERVICE | PENDING:
            continue
        assert calls & {require_admin, require_fresh_admin}, f"{method} {path} mutavel sem exigir admin"


def test_pending_routes_do_not_accept_full_sessions(app: FastAPI) -> None:
    for method, path, calls in _routes(app):
        if (method, path) in PENDING:
            assert require_pending in calls and require_user not in calls


def test_admin_route_inventory(app: FastAPI) -> None:
    admin = {(m, p) for m, p, calls in _routes(app) if calls & {require_admin, require_fresh_admin}}
    assert admin == EXPECTED_ADMIN_ROUTES


def test_sensitive_admin_routes_require_fresh_auth(app: FastAPI) -> None:
    fresh = {(m, p) for m, p, calls in _routes(app) if require_fresh_admin in calls}
    assert fresh == {
        ("POST", "/users"),
        ("PATCH", "/users/{user_id}"),
        ("POST", "/users/{user_id}/unlock"),
        ("POST", "/users/{user_id}/reset-two-factor"),
        ("POST", "/credentials"),
        ("PUT", "/credentials/{credential_id}"),
        ("DELETE", "/credentials/{credential_id}"),
        ("POST", "/control/reactivate"),
        ("POST", "/live/activate"),
        ("PUT", "/settings/{section}"),
        ("POST", "/ai/lessons/{lesson_id}/review"),
    }


def test_openapi_file_is_up_to_date() -> None:
    assert OPENAPI_PATH.read_text(encoding="utf-8") == build_schema_text(), (
        "api/openapi.json desatualizado: rode `uv run python -m api.export_openapi`"
    )


def test_openapi_is_deterministic() -> None:
    assert build_schema_text() == build_schema_text()


def test_operation_ids_are_unique_and_stable_camel_case(app: FastAPI) -> None:
    schema = json.loads(build_schema_text())
    ids = [op["operationId"] for item in schema["paths"].values() for op in item.values()]
    assert len(ids) == len(set(ids))
    assert all(re.fullmatch(r"[a-z][A-Za-z0-9]+", i) for i in ids)
    assert {"login", "verifySecondFactor", "createCredential", "listAuditLog", "getHealth"} <= set(ids)
    assert all(op.get("tags") for item in schema["paths"].values() for op in item.values())


def test_openapi_documents_problem_responses_and_security() -> None:
    schema = json.loads(build_schema_text())
    assert "Problem" in schema["components"]["schemas"]
    login = schema["paths"]["/auth/login"]["post"]
    assert "security" not in login and "application/problem+json" in login["responses"]["401"]["content"]
    create = schema["paths"]["/credentials"]["post"]
    assert create["security"] == [{"cookieAuth": [], "csrfToken": []}]
    assert schema["paths"]["/credentials"]["get"]["security"] == [{"cookieAuth": []}]


def test_no_response_schema_can_carry_secrets() -> None:
    schema = json.loads(build_schema_text())
    forbidden = {
        "api_secret",
        "password",
        "password_hash",
        "totp_secret",
        "token_hash",
        "api_key_enc",
        "api_secret_enc",
    }
    responses: set[str] = set()
    for item in schema["paths"].values():
        for op in item.values():
            for response in op["responses"].values():
                for media in response.get("content", {}).values():
                    ref = json.dumps(media)
                    responses.update(re.findall(r"#/components/schemas/(\w+)", ref))
    assert responses
    for name in responses:
        props = set(schema["components"]["schemas"][name].get("properties", {}))
        assert not props & forbidden, f"{name} expoe campo sensivel"
        assert "api_key" not in props, f"{name} expoe a chave inteira"
