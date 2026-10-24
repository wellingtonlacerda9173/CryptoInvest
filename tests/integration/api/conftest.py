import secrets
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import httpx
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.app import create_app
from api.audit import SYSTEM_META, AuditService
from api.binance_keys import FakeKeyVerifier
from api.config import ApiSettings
from api.container import Container
from api.security import totp
from api.services.users import UserService
from tests.api_support import GOOD_PASSWORD, FakeClock, make_settings


@dataclass(slots=True)
class Harness:
    app: FastAPI
    container: Container
    clock: FakeClock
    verifier: FakeKeyVerifier
    sessions: async_sessionmaker[AsyncSession]
    totp_secrets: dict[str, str] = field(default_factory=dict)
    activation_tokens: dict[str, str] = field(default_factory=dict)

    @property
    def settings(self) -> ApiSettings:
        return self.container.settings

    def client(self, *, ip: str = "203.0.113.9", **kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app, client=(ip, 4000)),
            base_url="http://testserver",
            headers={"User-Agent": "pytest-client"},
            **kwargs,
        )

    async def create_user(self, role: str = "reader", password: str = GOOD_PASSWORD) -> str:
        username = f"{role}.{secrets.token_hex(4)}"
        created = await UserService(self.container, AuditService(self.sessions)).create(
            username=username, password=password, role=role, actor="test", meta=SYSTEM_META
        )
        self.activation_tokens[username] = created.activation.token
        return username

    def code_for(self, username: str, *, step: int = 0) -> str:
        return totp.current_code(self.totp_secrets[username], self.clock() + timedelta(seconds=30 * step))

    async def login(
        self, username: str, password: str = GOOD_PASSWORD, *, ip: str = "203.0.113.9"
    ) -> "Session":
        """Login completo (enrola o 2FA na primeira vez). Cada TOTP avanca 30 s para nao colidir com o anti-replay."""
        client = self.client(ip=ip)
        first = await client.post("/auth/login", json={"username": username, "password": password})
        assert first.status_code == 200, first.text
        stage = first.json()["stage"]
        csrf = first.json()["csrf_token"]
        recovery: list[str] = []
        if stage == "enrollment":
            begin = await client.post(
                "/auth/2fa/enroll",
                json={"activation_token": self.activation_tokens[username]},
                headers={"X-CSRF-Token": csrf},
            )
            assert begin.status_code == 200, begin.text
            self.totp_secrets[username] = begin.json()["secret"]
            self.clock.advance(seconds=30)
            done = await client.post(
                "/auth/2fa/enroll/confirm",
                json={"code": self.code_for(username)},
                headers={"X-CSRF-Token": csrf},
            )
            assert done.status_code == 200, done.text
            recovery = done.json()["recovery_codes"]
            csrf = done.json()["csrf_token"]
        else:
            self.clock.advance(seconds=30)
            done = await client.post(
                "/auth/2fa/verify", json={"code": self.code_for(username)}, headers={"X-CSRF-Token": csrf}
            )
            assert done.status_code == 200, done.text
            csrf = done.json()["csrf_token"]
        return Session(client=client, csrf=csrf, username=username, recovery_codes=recovery)


@dataclass(slots=True)
class Session:
    client: httpx.AsyncClient
    csrf: str
    username: str
    recovery_codes: list[str]

    @property
    def headers(self) -> dict[str, str]:
        return {"X-CSRF-Token": self.csrf}

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.client.get(url, **kwargs)

    async def send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        return await self.client.request(method, url, headers=self.headers, **kwargs)

    async def aclose(self) -> None:
        await self.client.aclose()


@pytest_asyncio.fixture
async def harness(scratch_sessions: async_sessionmaker[AsyncSession]) -> AsyncIterator[Harness]:
    clock = FakeClock()
    verifier = FakeKeyVerifier()
    settings = make_settings()
    app = create_app(settings, sessions=scratch_sessions, verifier=verifier, clock=clock)
    container: Container = app.state.container
    assert container.clock is clock
    yield Harness(app=app, container=container, clock=clock, verifier=verifier, sessions=scratch_sessions)


@pytest_asyncio.fixture
async def admin(harness: Harness) -> AsyncIterator[Session]:
    session = await harness.login(await harness.create_user("admin"))
    yield session
    await session.aclose()


@pytest_asyncio.fixture
async def reader(harness: Harness) -> AsyncIterator[Session]:
    session = await harness.login(await harness.create_user("reader"))
    yield session
    await session.aclose()


def fake_key(prefix: str = "AKtest") -> tuple[str, str]:
    return f"{prefix}{uuid.uuid4().hex}"[:32], f"SECRET{uuid.uuid4().hex}{uuid.uuid4().hex}"[:48]
