"""Cabecalhos, CORS, limite de corpo, problem+json e acesso sem sessao: sem banco."""

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr, ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker

from api.app import create_app
from api.audit import sanitize
from tests.api_support import MASTER_KEY_V1, make_settings

PROTECTED_SAMPLE = [
    ("GET", "/auth/me"),
    ("POST", "/auth/logout"),
    ("GET", "/sessions"),
    ("GET", "/users"),
    ("GET", "/audit"),
    ("GET", "/credentials"),
    ("POST", "/credentials"),
    ("POST", "/auth/2fa/verify"),
]


def client_for(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


@pytest.fixture
def app() -> FastAPI:
    return create_app(make_settings(hsts_max_age_seconds=31536000), sessions=async_sessionmaker())


async def test_security_headers_and_request_id(app: FastAPI) -> None:
    async with client_for(app) as client:
        response = await client.get("/health", headers={"X-Request-ID": "abc-12345678"})
    assert response.status_code == 200 and response.json() == {"status": "ok"}
    headers = response.headers
    assert headers["x-request-id"] == "abc-12345678"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert headers["referrer-policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert headers["cache-control"] == "no-store"
    assert headers["strict-transport-security"].startswith("max-age=31536000")


async def test_malicious_request_id_is_replaced(app: FastAPI) -> None:
    async with client_for(app) as client:
        response = await client.get("/health", headers={"X-Request-ID": "x\r\nSet-Cookie: a=b"})
    assert response.headers["x-request-id"] != "x"
    assert len(response.headers["x-request-id"]) == 32


async def test_hsts_is_off_by_default() -> None:
    app = create_app(make_settings(), sessions=async_sessionmaker())
    async with client_for(app) as client:
        response = await client.get("/health")
    assert "strict-transport-security" not in response.headers


@pytest.mark.parametrize(("method", "path"), PROTECTED_SAMPLE)
async def test_protected_routes_reject_missing_session(app: FastAPI, method: str, path: str) -> None:
    async with client_for(app) as client:
        response = await client.request(method, path, json={} if method == "POST" else None)
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["status"] == 401 and body["request_id"] == response.headers["x-request-id"]


async def test_garbage_cookie_is_unauthorized(app: FastAPI) -> None:
    async with client_for(app) as client:
        response = await client.get("/auth/me", cookies={"ci_session": "x" * 500})
    assert response.status_code == 401


async def test_not_found_and_method_not_allowed_are_problem_json(app: FastAPI) -> None:
    async with client_for(app) as client:
        missing = await client.get("/nao-existe")
        wrong = await client.delete("/health")
    assert missing.status_code == 404 and missing.headers["content-type"].startswith(
        "application/problem+json"
    )
    assert wrong.status_code == 405 and wrong.json()["title"] == "Method Not Allowed"


async def test_validation_error_never_echoes_input(app: FastAPI) -> None:
    secret = "SEGREDO-QUE-NAO-PODE-VAZAR-12345"
    async with client_for(app) as client:
        response = await client.post(
            "/auth/login", json={"username": "ab", "password": secret, "extra": secret}
        )
    assert response.status_code == 422
    assert secret not in response.text
    assert "input" not in response.text
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["errors"][0]["loc"] == ["username"]


async def test_invalid_json_is_problem_json(app: FastAPI) -> None:
    async with client_for(app) as client:
        response = await client.post(
            "/auth/login", content=b"{nao json", headers={"Content-Type": "application/json"}
        )
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")


async def test_body_limit_by_content_length_and_streaming(app: FastAPI) -> None:
    async with client_for(app) as client:
        big = await client.post("/auth/login", content=b"x" * (64 * 1024 + 1))
        assert big.status_code == 413
        assert big.headers["content-type"].startswith("application/problem+json")

        async def chunks():  # type: ignore[no-untyped-def]
            for _ in range(70):
                yield b"y" * 1024

        streamed = await client.post(
            "/auth/login", content=chunks(), headers={"Content-Type": "application/json"}
        )
    assert streamed.status_code == 413


async def test_cors_is_strict(app: FastAPI) -> None:
    async with client_for(app) as client:
        ok = await client.options(
            "/auth/me",
            headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "GET"},
        )
        bad = await client.options(
            "/auth/me", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"}
        )
        simple = await client.get("/health", headers={"Origin": "https://evil.example"})
    assert ok.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert ok.headers["access-control-allow-credentials"] == "true"
    assert "access-control-allow-origin" not in bad.headers
    assert "access-control-allow-origin" not in simple.headers


async def test_mutation_from_foreign_origin_is_forbidden(app: FastAPI) -> None:
    async with client_for(app) as client:
        response = await client.post(
            "/auth/login",
            json={"username": "ana", "password": "x"},
            headers={"Origin": "https://evil.example"},
        )
    assert response.status_code == 403


async def test_ready_reports_unavailable_without_database(app: FastAPI) -> None:
    async with client_for(app) as client:
        response = await client.get("/ready")
    assert response.status_code == 503 and response.json() == {"status": "unavailable", "database": False}


async def test_unhandled_error_is_generic_problem_json() -> None:
    app = create_app(make_settings(), sessions=async_sessionmaker())

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("senha=hunter2 vazou")

    async with client_for(app) as client:
        response = await client.get("/boom")
    assert response.status_code == 500
    assert "hunter2" not in response.text and "RuntimeError" not in response.text
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.json()["request_id"] == response.headers["x-request-id"]


def test_settings_reject_wildcard_and_bad_origins() -> None:
    for origin in ("*", "localhost:3000", "http://a.com/path", "ftp://a.com", "http://user@a.com"):
        with pytest.raises(ValidationError):
            make_settings(panel_origins=[origin])


def test_production_guards() -> None:
    with pytest.raises(ValidationError):
        make_settings(environment="prod", cookie_secure=False, panel_origins=["https://painel.example.com"])
    with pytest.raises(ValidationError):
        make_settings(environment="prod", cookie_secure=True, panel_origins=["http://painel.example.com"])
    with pytest.raises(ValidationError):
        make_settings(environment="prod", cookie_secure=True, panel_origins=["https://p.example.com"])
    prod = make_settings(
        environment="prod",
        cookie_secure=True,
        panel_origins=["https://p.example.com/"],
        hsts_max_age_seconds=3600,
        database_user="cryptoinvest_api",
        database_password=SecretStr("senha-do-papel-da-api"),
    )
    assert prod.panel_origins == ["https://p.example.com"]
    assert prod.session_cookie_name.startswith("__Host-")


def test_short_session_secret_is_rejected() -> None:
    with pytest.raises(ValidationError):
        make_settings(session_secret=SecretStr("curto"))


def test_secrets_are_read_from_files_only(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    from api.config import ApiSettings

    (tmp_path / "api_session_secret").write_text("s" * 48)
    (tmp_path / "api_master_keys").write_text(f"1:{MASTER_KEY_V1}\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("API_SESSION_SECRET", "from-env-" + "e" * 40)
    settings = ApiSettings(_secrets_dir=tmp_path)  # type: ignore[call-arg]
    assert settings.session_secret.get_secret_value() == "s" * 48
    assert "s" * 48 not in repr(settings) and MASTER_KEY_V1 not in repr(settings)


def test_audit_sanitize_masks_secrets() -> None:
    cleaned = sanitize(
        {
            "password": "p",
            "api_secret": "s",
            "totp_code": "123456",
            "key_last4": "a91f",
            "note": "falha em token=abc123 e senha ok",
            "nested": {"authorization": "Bearer abc", "label": "Conta"},
            "items": [{"secret": "x"}],
        }
    )
    assert cleaned["password"] == cleaned["api_secret"] == cleaned["totp_code"] == "***"
    assert cleaned["key_last4"] == "a91f"
    assert "abc123" not in cleaned["note"]
    assert cleaned["nested"] == {"authorization": "***", "label": "Conta"}
    assert cleaned["items"] == [{"secret": "***"}]
