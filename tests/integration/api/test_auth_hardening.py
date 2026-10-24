"""Endurecimento do login: desbloqueio entre processos, senha trocada durante o login e travas do reencrypt."""

import asyncio

import pytest
from sqlalchemy import select, update

from api.app import create_app
from api.audit import SYSTEM_META, AuditService
from api.cli import REQUIRED_API_UPDATES
from api.persistence.models import UserRow
from api.services.maintenance import reencrypt_all
from api.services.users import UserService
from tests.api_support import GOOD_PASSWORD, OTHER_PASSWORD, make_settings
from tests.integration.api.conftest import Harness, Session

pytestmark = pytest.mark.integration


async def _lock_out(harness: Harness, name: str, ip: str) -> None:
    attacker = harness.client(ip=ip)
    for _ in range(6):
        await attacker.post("/auth/login", json={"username": name, "password": "errada errada 123!"})
    locked = await attacker.post("/auth/login", json={"username": name, "password": GOOD_PASSWORD})
    assert locked.status_code == 429


async def test_unlock_from_another_process_releases_the_password_stage_lock(harness: Harness) -> None:
    """`unlock-user` da CLI roda em OUTRO processo: so o banco (lock_epoch) chega ao processo da API."""
    name = await harness.create_user("reader")
    ip = "192.0.2.150"
    await _lock_out(harness, name, ip)

    cli_app = create_app(
        make_settings(), sessions=harness.sessions, verifier=harness.verifier, clock=harness.clock
    )
    cli_container = cli_app.state.container
    assert cli_container is not harness.container
    await UserService(cli_container, AuditService(harness.sessions)).unlock_username(
        username=name, actor="cli:test", meta=SYSTEM_META, via="cli"
    )

    again = await harness.client(ip=ip).post(
        "/auth/login", json={"username": name, "password": GOOD_PASSWORD}
    )
    assert again.status_code == 200, again.text


async def test_login_does_not_open_a_session_for_a_password_changed_meanwhile(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    name = await harness.create_user("reader")
    original = harness.container.passwords.verify
    changed = asyncio.Event()

    async def verify_then_lose_the_race(stored_hash: str | None, password: str) -> bool:
        ok = await original(stored_hash, password)
        if not changed.is_set():
            changed.set()
            fresh = await harness.container.passwords.hash(OTHER_PASSWORD)
            async with harness.sessions.begin() as db:
                await db.execute(update(UserRow).where(UserRow.username == name).values(password_hash=fresh))
        return ok

    monkeypatch.setattr(harness.container.passwords, "verify", verify_then_lose_the_race)
    response = await harness.client(ip="192.0.2.152").post(
        "/auth/login", json={"username": name, "password": GOOD_PASSWORD}
    )
    assert response.status_code == 401


async def test_change_password_with_a_stale_hash_conflicts_instead_of_overwriting(
    harness: Harness, reader: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = harness.container.passwords.verify

    async def verify_then_change_elsewhere(stored_hash: str | None, password: str) -> bool:
        ok = await original(stored_hash, password)
        fresh = await harness.container.passwords.hash(OTHER_PASSWORD)
        async with harness.sessions.begin() as db:
            await db.execute(
                update(UserRow).where(UserRow.username == reader.username).values(password_hash=fresh)
            )
        return ok

    monkeypatch.setattr(harness.container.passwords, "verify", verify_then_change_elsewhere)
    response = await reader.send(
        "POST",
        "/auth/password",
        json={"current_password": GOOD_PASSWORD, "new_password": "uma frase totalmente nova 2026!"},
    )
    assert response.status_code == 409, response.text
    monkeypatch.setattr(harness.container.passwords, "verify", original)
    async with harness.sessions() as db:
        stored = await db.scalar(select(UserRow.password_hash).where(UserRow.username == reader.username))
    assert stored is not None and await original(stored, OTHER_PASSWORD)


async def test_two_concurrent_reencrypts_do_not_deadlock(harness: Harness, admin: Session) -> None:
    for _ in range(3):
        await harness.login(await harness.create_user("reader"))
    audit = AuditService(harness.sessions)
    reports = await asyncio.wait_for(
        asyncio.gather(
            *(reencrypt_all(harness.container, audit, actor=f"test:{i}") for i in range(3)),
        ),
        timeout=30,
    )
    assert all(not report.failed for report in reports)


def test_check_grants_lists_every_column_the_api_updates() -> None:
    assert "lock_epoch" in REQUIRED_API_UPDATES["users"]
    assert all(columns for columns in REQUIRED_API_UPDATES.values())
