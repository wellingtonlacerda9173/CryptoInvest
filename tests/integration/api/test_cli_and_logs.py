import argparse
import io
import sys
import types

import pytest
from sqlalchemy import URL, select, text

from api import cli
from api.persistence.models import UserRow
from tests.api_support import GOOD_PASSWORD, make_settings
from tests.integration.api.conftest import Harness, fake_key
from tests.integration.disposable_db import assert_disposable_database

pytestmark = pytest.mark.integration


async def test_cli_create_admin_bootstrap_and_unlock(
    scratch_url: URL, scratch_sessions, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert_disposable_database(scratch_url)
    monkeypatch.setattr(cli, "load_settings", lambda: types.SimpleNamespace(database_url=scratch_url))
    settings = make_settings()
    username = "cli.admin.test"

    monkeypatch.setattr(sys, "stdin", io.StringIO(GOOD_PASSWORD + "\n"))
    args = argparse.Namespace(username=username, password_stdin=True, allow_additional=True)
    assert await cli.create_admin(args, settings) == 0
    printed = capsys.readouterr().out
    token = printed.split("UMA vez; repasse fora de banda, nao junto da senha): ")[1].split()[0]
    assert len(token) >= 32
    async with scratch_sessions() as db:
        user = await db.scalar(select(UserRow).where(UserRow.username == username))
    assert user is not None and user.role == "admin" and user.totp_enabled is False
    assert user.activation_token_hash and token not in user.activation_token_hash
    assert user.password_hash.startswith("$argon2id$") and GOOD_PASSWORD not in user.password_hash

    # sem --allow-additional, recusa quando ja ha usuarios
    monkeypatch.setattr(sys, "stdin", io.StringIO(GOOD_PASSWORD + "\n"))
    refused = argparse.Namespace(username="outro.admin", password_stdin=True, allow_additional=False)
    assert await cli.create_admin(refused, settings) == 1
    assert "Ja existem usuarios" in capsys.readouterr().err

    # senha fraca e recusada sem eco
    monkeypatch.setattr(sys, "stdin", io.StringIO("password1234\n"))
    weak = argparse.Namespace(username="fraco.admin", password_stdin=True, allow_additional=True)
    assert await cli.create_admin(weak, settings) == 1
    assert "password1234" not in capsys.readouterr().err

    async with scratch_sessions.begin() as db:
        await db.execute(
            text("UPDATE users SET failed_attempts = 7, locked_until = now() WHERE username = :u"),
            {"u": username},
        )
    assert await cli.unlock_user(argparse.Namespace(username=username), settings) == 0
    async with scratch_sessions() as db:
        unlocked = await db.scalar(select(UserRow).where(UserRow.username == username))
    assert unlocked is not None and unlocked.failed_attempts == 0 and unlocked.locked_until is None


async def test_logs_never_contain_passwords_codes_or_keys(
    harness: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    username = await harness.create_user("admin")
    session = await harness.login(username)
    key, secret = fake_key()
    created = await session.send(
        "POST", "/credentials", json={"label": "Log test", "api_key": key, "api_secret": secret}
    )
    assert created.status_code == 201
    other = harness.client()
    await other.post("/auth/login", json={"username": username, "password": "senha errada 123456"})
    await other.aclose()
    await session.aclose()
    out = capsys.readouterr()
    logged = out.out + out.err
    assert "http_request" in logged
    for forbidden in (
        GOOD_PASSWORD,
        "senha errada 123456",
        key,
        secret,
        harness.totp_secrets[username],
        session.csrf,
    ):
        assert forbidden not in logged
    assert "/credentials" in logged and "?" not in logged.split("path=/credentials")[1].split()[0]
