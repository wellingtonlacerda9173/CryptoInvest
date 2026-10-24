"""Reverificacao (2a rodada) da seguranca da API: ataques as correcoes da 1a rodada.

Sondas de regressao: itens que eram `xfail(strict=True)` e ja foram corrigidos na producao.
"""

import asyncio
import contextlib
import time
import uuid
from contextvars import ContextVar

import pytest
from alembic import command
from alembic.config import Config
from pydantic import SecretStr
from sqlalchemy import URL, event, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from api.app import create_app
from api.audit import SYSTEM_META, AuditService
from api.persistence.models import UserRow
from api.services.auth import AuthService
from api.services.sessions import Principal
from api.services.users import UserService
from engine.infrastructure.persistence.models import AuditLogRow
from tests.api_support import GOOD_PASSWORD, make_settings
from tests.integration.api.conftest import Harness, Session
from tests.integration.disposable_db import assert_disposable_database

pytestmark = pytest.mark.integration


async def _user_id(harness: Harness, username: str) -> uuid.UUID:
    async with harness.sessions() as db:
        found = await db.scalar(select(UserRow.id).where(UserRow.username == username))
    assert found is not None
    return found


# ----------------------------------------------------------------------------- ativacao do 2FA


async def test_activation_token_of_another_user_cannot_enroll(harness: Harness) -> None:
    victim = await harness.create_user("reader")
    other = await harness.create_user("reader")
    client = harness.client(ip="192.0.2.90")
    step = await client.post("/auth/login", json={"username": victim, "password": GOOD_PASSWORD})
    response = await client.post(
        "/auth/2fa/enroll",
        json={"activation_token": harness.activation_tokens[other]},
        headers={"X-CSRF-Token": step.json()["csrf_token"]},
    )
    assert response.status_code == 401
    async with harness.sessions() as db:
        row = await db.scalar(select(UserRow).where(UserRow.username == victim))
    assert row is not None and row.totp_secret is None and not row.totp_enabled


async def test_wrong_activation_tokens_kill_the_pending_session_and_lock_the_account(
    harness: Harness,
) -> None:
    username = await harness.create_user("reader")
    client = harness.client(ip="192.0.2.91")
    step = await client.post("/auth/login", json={"username": username, "password": GOOD_PASSWORD})
    headers = {"X-CSRF-Token": step.json()["csrf_token"]}
    codes = [
        (
            await client.post("/auth/2fa/enroll", json={"activation_token": "z" * 32}, headers=headers)
        ).status_code
        for _ in range(6)
    ]
    assert codes[:5] == [401] * 5 and codes[5] == 401, codes  # a sessao pendente morre na 5a falha
    good = await client.post(
        "/auth/2fa/enroll", json={"activation_token": harness.activation_tokens[username]}, headers=headers
    )
    assert good.status_code == 401, "com a sessao pendente revogada nem o token certo funciona"


async def test_confirm_with_a_guessed_code_never_activates_without_the_enrolled_secret(
    harness: Harness,
) -> None:
    username = await harness.create_user("reader")
    legit = harness.client(ip="192.0.2.92")
    step = await legit.post("/auth/login", json={"username": username, "password": GOOD_PASSWORD})
    started = await legit.post(
        "/auth/2fa/enroll",
        json={"activation_token": harness.activation_tokens[username]},
        headers={"X-CSRF-Token": step.json()["csrf_token"]},
    )
    assert started.status_code == 200
    # outra sessao pendente (so a senha, sem o token) tenta confirmar com um codigo qualquer
    intruder = harness.client(ip="192.0.2.93")
    other = await intruder.post("/auth/login", json={"username": username, "password": GOOD_PASSWORD})
    response = await intruder.post(
        "/auth/2fa/enroll/confirm",
        json={"code": "000000"},
        headers={"X-CSRF-Token": other.json()["csrf_token"]},
    )
    assert response.status_code == 401
    async with harness.sessions() as db:
        row = await db.scalar(select(UserRow).where(UserRow.username == username))
    assert row is not None and not row.totp_enabled


async def test_reset_two_factor_of_an_enrolling_user_invalidates_the_old_token_and_secret(
    harness: Harness, admin: Session
) -> None:
    username = await harness.create_user("reader")
    client = harness.client(ip="192.0.2.94")
    step = await client.post("/auth/login", json={"username": username, "password": GOOD_PASSWORD})
    started = await client.post(
        "/auth/2fa/enroll",
        json={"activation_token": harness.activation_tokens[username]},
        headers={"X-CSRF-Token": step.json()["csrf_token"]},
    )
    old_secret = started.json()["secret"]
    user_id = await _user_id(harness, username)
    reset = await admin.send("POST", f"/users/{user_id}/reset-two-factor")
    assert reset.status_code == 200
    harness.totp_secrets[username] = old_secret
    harness.clock.advance(seconds=30)
    stale = await client.post(
        "/auth/2fa/enroll/confirm",
        json={"code": harness.code_for(username)},
        headers={"X-CSRF-Token": step.json()["csrf_token"]},
    )
    assert stale.status_code == 401, "a sessao pendente anterior foi revogada pelo reset"


# ----------------------------------------------------------------------------- recovery codes legados


async def test_legacy_recovery_hashes_are_accepted_only_while_the_flag_is_on(harness: Harness) -> None:
    secret = harness.settings.session_secret
    new_key = SecretStr("k" * 48)
    username = await harness.create_user("reader")
    first = await harness.login(username)  # codigos gerados com o session_secret (legado)
    codes = first.recovery_codes
    await first.aclose()

    async def attempt(accept_legacy: bool, code: str) -> int:
        settings = make_settings(
            session_secret=secret, recovery_hmac_key=new_key, recovery_accept_legacy_hashes=accept_legacy
        )
        app = create_app(settings, sessions=harness.sessions, verifier=harness.verifier, clock=harness.clock)
        probe = Harness(
            app=app,
            container=app.state.container,
            clock=harness.clock,
            verifier=harness.verifier,
            sessions=harness.sessions,
            totp_secrets=harness.totp_secrets,
        )
        client = probe.client(ip=f"192.0.2.{100 + int(accept_legacy)}")
        step = await client.post("/auth/login", json={"username": username, "password": GOOD_PASSWORD})
        done = await client.post(
            "/auth/2fa/verify",
            json={"recovery_code": code},
            headers={"X-CSRF-Token": step.json()["csrf_token"]},
        )
        await client.aclose()
        return done.status_code

    assert await attempt(False, codes[0]) == 401
    assert await attempt(True, codes[0]) == 200
    assert await attempt(True, codes[0]) == 401, "o codigo legado tambem e de uso unico"


# ----------------------------------------------------------------------------- bloqueio x desbloqueio


async def test_admin_unlock_releases_the_password_stage_lock(harness: Harness, admin: Session) -> None:
    name = await harness.create_user("reader")
    ip = "192.0.2.110"
    attacker = harness.client(ip=ip)
    for _ in range(6):
        await attacker.post("/auth/login", json={"username": name, "password": "errada errada 123!"})
    locked = await attacker.post("/auth/login", json={"username": name, "password": GOOD_PASSWORD})
    assert locked.status_code == 429
    unlock = await admin.send("POST", f"/users/{await _user_id(harness, name)}/unlock")
    assert unlock.status_code == 200
    again = await attacker.post("/auth/login", json={"username": name, "password": GOOD_PASSWORD})
    assert again.status_code == 200, again.status_code


# ----------------------------------------------------------------------------- IPv6


async def test_one_ipv6_prefix_shares_the_failure_budget(harness: Harness) -> None:
    name = await harness.create_user("reader")
    for i in range(6):
        client = harness.client(ip=f"2001:db8:abcd:12::{i + 1:x}")
        await client.post("/auth/login", json={"username": name, "password": "errada errada 123!"})
    blocked = await harness.client(ip="2001:db8:abcd:12::ffff").post(
        "/auth/login", json={"username": name, "password": "errada errada 123!"}
    )
    assert blocked.status_code == 429, "6 falhas do mesmo /64 contra o mesmo usuario deveriam bloquear"


# ----------------------------------------------------------------------------- Argon2 x pool de conexoes


async def test_change_password_burst_does_not_exhaust_the_connection_pool(
    harness: Harness, reader: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Argon2 (verificar e gerar) nunca roda com conexao do pool em maos, e a rajada nao segura o pool.

    Medir `pool.checkedout()` por amostragem confunde a chegada simultanea das requisicoes (autenticacao
    enche o pool por milissegundos, e muito mais numa maquina carregada) com SEGURAR o pool. Aqui cada
    conexao e atribuida a requisicao que a pegou (eventos checkout/checkin do pool): dentro do hash a
    requisicao nao pode ter nenhuma."""
    # contextvar (e nao a tarefa): o ASGI/middleware faz checkout e checkin em tarefas diferentes da mesma requisicao
    held_by_request: ContextVar[list[int] | None] = ContextVar("held_by_request", default=None)
    engine = harness.sessions.kw["bind"]

    def _bump(delta: int) -> None:
        if (box := held_by_request.get()) is not None:
            box[0] += delta

    event.listen(engine.sync_engine.pool, "checkout", lambda *_: _bump(1))
    event.listen(engine.sync_engine.pool, "checkin", lambda *_: _bump(-1))
    violations: list[str] = []
    in_hash = peak_in_hash = 0
    original_verify = harness.container.passwords.verify
    original_hash = harness.container.passwords.hash

    async def observed(stage: str) -> None:
        nonlocal in_hash, peak_in_hash
        if (box := held_by_request.get()) is not None and box[0] > 0:
            violations.append(f"{stage} com {box[0]} conexao(oes) do pool em maos")
        in_hash += 1
        peak_in_hash = max(peak_in_hash, in_hash)
        await asyncio.sleep(0.15)
        in_hash -= 1

    async def slow_verify(stored_hash: str | None, password: str) -> bool:
        await observed("verify")
        return await original_verify(stored_hash, password)

    async def slow_hash(password: str) -> str:
        await observed("hash")
        return await original_hash(password)

    monkeypatch.setattr(harness.container.passwords, "verify", slow_verify)
    monkeypatch.setattr(harness.container.passwords, "hash", slow_hash)
    await _warm_pool(harness, 15)

    async def change(i: int, current: str) -> int:
        held_by_request.set([0])
        client = harness.client(ip=f"10.8.{i // 200}.{i % 200 + 1}")
        client.cookies.update(reader.client.cookies)
        r = await client.post(
            "/auth/password",
            json={"current_password": current, "new_password": "outra senha bem forte 77?"},
            headers=reader.headers,
        )
        await client.aclose()
        return r.status_code

    codes = await asyncio.gather(*(change(i, "errada errada errada 1!") for i in range(40)))
    assert set(codes) <= {401, 429}, sorted(set(codes))
    assert not violations, violations[:3]
    assert peak_in_hash <= harness.settings.login_max_concurrent_hashes
    # o caminho feliz tambem hasheia a senha nova fora da transacao
    held_by_request.set([0])
    happy = await reader.send(
        "POST",
        "/auth/password",
        json={"current_password": GOOD_PASSWORD, "new_password": "outra senha bem forte 77?"},
    )
    assert happy.status_code == 204, happy.text
    assert not violations, violations[:3]


# ----------------------------------------------------------------------------- HashGate


async def test_distributed_login_flood_makes_legitimate_login_fail_fast_not_hang(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Limite de projeto (nao e defeito): a fila e curta e a recusa e imediata; o admin ja logado segue ok."""
    username = await harness.create_user("reader")
    original = harness.container.passwords.verify

    async def slow_verify(stored_hash: str | None, password: str) -> bool:
        await asyncio.sleep(0.4)
        return await original(stored_hash, password)

    monkeypatch.setattr(harness.container.passwords, "verify", slow_verify)

    async def flood(i: int) -> int:
        client = harness.client(ip=f"10.7.{i // 200}.{i % 200 + 1}")
        r = await client.post("/auth/login", json={"username": f"x{i}.nobody", "password": "y" * 14})
        await client.aclose()
        return r.status_code

    burst = asyncio.gather(*(flood(i) for i in range(80)))
    await asyncio.sleep(0.1)
    started = time.monotonic()
    legit = await harness.client(ip="203.0.113.50").post(
        "/auth/login", json={"username": username, "password": GOOD_PASSWORD}
    )
    elapsed = time.monotonic() - started
    await burst
    assert legit.status_code in {200, 429}
    assert legit.status_code == 200 or legit.headers.get("retry-after"), "a recusa traz Retry-After"
    assert elapsed < 15.0, (
        "recusa rapida, nunca pendurada (folga larga: a maquina de CI pode estar carregada)"
    )


# ----------------------------------------------------------------------------- admins / usuarios


async def _warm_pool(harness: Harness, connections: int = 4) -> None:
    """Abre varias conexoes AO MESMO TEMPO e as devolve ao pool: a 2a transacao nao paga o handshake do banco
    (>1 s nesta maquina), o que fazia a barreira de 1 s do teste antigo estourar e esconder a corrida."""

    async def hold() -> None:
        async with harness.sessions() as db:
            await db.execute(text("SELECT pg_sleep(0.3)"))

    await asyncio.gather(*(hold() for _ in range(connections)))


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ({"role": "reader"}, {"role": "reader"}),
        ({"disabled": True}, {"role": "reader"}),
        ({"disabled": True}, {"disabled": True}),
    ],
)
async def test_crossed_admin_removals_with_forced_interleaving_leave_an_admin(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, first: dict[str, object], second: dict[str, object]
) -> None:
    """Forca as duas transacoes a contarem os admins ANTES de qualquer commit (pool aquecido, espera de 4 s):
    sem a trava dos admins ativos as duas passam e o sistema fica sem administrador."""
    async with harness.sessions.begin() as db:
        await db.execute(text("UPDATE users SET role = 'reader' WHERE role = 'admin'"))
    one = await harness.create_user("admin")
    two = await harness.create_user("admin")
    ids = {name: await _user_id(harness, name) for name in (one, two)}
    await _warm_pool(harness)

    barrier = asyncio.Barrier(2)
    original = UserService._active_admins

    async def counted_then_wait(self: UserService, db: AsyncSession) -> int:
        count = await original(self, db)
        with contextlib.suppress(TimeoutError, asyncio.BrokenBarrierError):
            await asyncio.wait_for(barrier.wait(), 4.0)
        return count

    monkeypatch.setattr(UserService, "_active_admins", counted_then_wait)
    service = UserService(harness.container, AuditService(harness.sessions))
    results = await asyncio.wait_for(
        asyncio.gather(
            service.update(
                user_id=ids[two],
                role=first.get("role"),
                disabled=first.get("disabled"),  # type: ignore[arg-type]
                actor_id=ids[one],
                actor=one,
                meta=SYSTEM_META,
            ),
            service.update(
                user_id=ids[one],
                role=second.get("role"),
                disabled=second.get("disabled"),  # type: ignore[arg-type]
                actor_id=ids[two],
                actor=two,
                meta=SYSTEM_META,
            ),
            return_exceptions=True,
        ),
        timeout=30,
    )
    async with harness.sessions() as db:
        remaining = (
            await db.scalars(select(UserRow).where(UserRow.role == "admin", UserRow.disabled.is_(False)))
        ).all()
    assert len(remaining) >= 1, results
    assert sum(isinstance(r, Exception) for r in results) >= 1, results


async def test_concurrent_totp_replay_with_warm_pool_yields_single_success(harness: Harness) -> None:
    """O teste antigo roda com pool frio: o handshake (>1 s) serializa as requisicoes e esconde a corrida."""
    username = await harness.create_user("admin")
    await (await harness.login(username)).aclose()
    clients = [harness.client(ip=f"192.0.2.{140 + i}") for i in range(6)]
    steps = [
        await c.post("/auth/login", json={"username": username, "password": GOOD_PASSWORD}) for c in clients
    ]
    await _warm_pool(harness, 10)
    harness.clock.advance(seconds=30)
    code = harness.code_for(username)
    results = await asyncio.gather(
        *(
            c.post("/auth/2fa/verify", json={"code": code}, headers={"X-CSRF-Token": s.json()["csrf_token"]})
            for c, s in zip(clients, steps, strict=True)
        )
    )
    assert sorted(r.status_code for r in results).count(200) == 1, [r.status_code for r in results]


async def test_concurrent_totp_replay_with_forced_interleaving_yields_single_success(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Todas as requisicoes LEEM o usuario antes de qualquer commit (barreira apos `_load_pending`): so o lock
    de linha OU o UPDATE condicional do contador impede o replay; com o lock as demais esperam (barreira de 4 s)."""
    username = await harness.create_user("admin")
    await (await harness.login(username)).aclose()
    clients = [harness.client(ip=f"192.0.2.{170 + i}") for i in range(4)]
    steps = [
        await c.post("/auth/login", json={"username": username, "password": GOOD_PASSWORD}) for c in clients
    ]
    await _warm_pool(harness, 10)
    barrier = asyncio.Barrier(len(clients))
    original = AuthService._load_pending

    async def loaded_then_wait(self: AuthService, db: AsyncSession, principal: Principal):  # type: ignore[no-untyped-def]
        loaded = await original(self, db, principal)
        with contextlib.suppress(TimeoutError, asyncio.BrokenBarrierError):
            await asyncio.wait_for(barrier.wait(), 4.0)
        return loaded

    monkeypatch.setattr(AuthService, "_load_pending", loaded_then_wait)
    harness.clock.advance(seconds=30)
    code = harness.code_for(username)
    results = await asyncio.gather(
        *(
            c.post("/auth/2fa/verify", json={"code": code}, headers={"X-CSRF-Token": s.json()["csrf_token"]})
            for c, s in zip(clients, steps, strict=True)
        )
    )
    assert sorted(r.status_code for r in results).count(200) == 1, [r.status_code for r in results]


async def test_concurrent_recovery_code_with_warm_pool_yields_single_success(harness: Harness) -> None:
    username = await harness.create_user("admin")
    first = await harness.login(username)
    code = first.recovery_codes[0]
    await first.aclose()
    clients = [harness.client(ip=f"192.0.2.{150 + i}") for i in range(6)]
    steps = [
        await c.post("/auth/login", json={"username": username, "password": GOOD_PASSWORD}) for c in clients
    ]
    await _warm_pool(harness, 10)
    results = await asyncio.gather(
        *(
            c.post(
                "/auth/2fa/verify",
                json={"recovery_code": code},
                headers={"X-CSRF-Token": s.json()["csrf_token"]},
            )
            for c, s in zip(clients, steps, strict=True)
        )
    )
    assert [r.status_code for r in results].count(200) == 1, [r.status_code for r in results]


async def test_login_burst_really_goes_through_the_hash_gate(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mata o mutante que tira o login do HashGate: `gate.peak <= max` do teste antigo vale 0 sem o gate."""
    original = harness.container.passwords.verify

    gate = harness.container.hash_gate

    async def slow_verify(stored_hash: str | None, password: str) -> bool:
        # segura os slots ate a fila recusar alguem: independe da velocidade com que a maquina entrega as requisicoes
        for _ in range(400):
            if gate.rejected:
                break
            await asyncio.sleep(0.05)
        return await original(stored_hash, password)

    monkeypatch.setattr(harness.container.passwords, "verify", slow_verify)
    await _warm_pool(harness, 10)

    async def attempt(i: int) -> int:
        client = harness.client(ip=f"10.6.{i // 200}.{i % 200 + 1}")
        r = await client.post("/auth/login", json={"username": f"y{i}.nobody", "password": "y" * 14})
        await client.aclose()
        return r.status_code

    codes = await asyncio.gather(*(attempt(i) for i in range(60)))
    assert gate.peak == harness.settings.login_max_concurrent_hashes
    assert 429 in codes and 401 in codes, sorted(set(codes))


async def test_role_change_revokes_the_target_sessions(harness: Harness, admin: Session) -> None:
    """Mata o mutante M19: a sessao do usuario alterado deixa de valer (nao so a leitura do papel em tempo real)."""
    other = await harness.login(await harness.create_user("reader"))
    other_id = (await other.get("/auth/me")).json()["id"]
    promoted = await admin.send("PATCH", f"/users/{other_id}", json={"role": "admin"})
    assert promoted.status_code == 200
    assert (await other.get("/auth/me")).status_code == 401
    await other.aclose()


async def test_demoted_admin_loses_admin_routes_immediately(harness: Harness, admin: Session) -> None:
    second = await harness.login(await harness.create_user("admin"))
    second_id = (await second.get("/auth/me")).json()["id"]
    assert (await admin.send("PATCH", f"/users/{second_id}", json={"role": "reader"})).status_code == 200
    assert (await second.get("/users")).status_code in {401, 403}
    await second.aclose()


async def test_unlock_requires_fresh_auth_even_for_the_cli_actor_model(
    harness: Harness, admin: Session
) -> None:
    target = await harness.create_user("reader")
    harness.clock.advance(minutes=harness.settings.fresh_auth_minutes + 1)
    response = await admin.send("POST", f"/users/{await _user_id(harness, target)}/unlock")
    assert response.status_code == 403 and response.json()["type"].endswith("fresh-auth-required")


# ----------------------------------------------------------------------------- auditoria de erros


async def test_activation_failures_are_audited_without_the_token(harness: Harness) -> None:
    username = await harness.create_user("reader")
    client = harness.client(ip="192.0.2.120")
    step = await client.post("/auth/login", json={"username": username, "password": GOOD_PASSWORD})
    attempt = "T0ken-que-nao-pode-vazar-na-auditoria"
    await client.post(
        "/auth/2fa/enroll",
        json={"activation_token": attempt},
        headers={"X-CSRF-Token": step.json()["csrf_token"]},
    )
    async with harness.sessions() as db:
        rows = (await db.scalars(select(AuditLogRow))).all()
    assert any(r.action == "auth.activation.failed" and r.actor == username for r in rows)
    assert all(attempt not in str(r.payload) for r in rows)


# ----------------------------------------------------------------------------- migracao 0008


def _config(url: URL) -> Config:
    assert_disposable_database(url)
    config = Config("alembic.ini")
    config.attributes["database_url"] = url
    return config


def test_migration_0008_keeps_existing_users_and_downgrades_cleanly(migration_url: URL) -> None:
    config = _config(migration_url)
    command.upgrade(config, "0007")

    async def run(sql: str) -> list[tuple[object, ...]]:
        engine = create_async_engine(migration_url)
        try:
            async with engine.begin() as conn:
                result = await conn.execute(text(sql))
                return [tuple(r) for r in result] if result.returns_rows else []
        finally:
            await engine.dispose()

    def sql(statement: str) -> list[tuple[object, ...]]:
        return asyncio.run(run(statement))

    sql(
        "INSERT INTO users (id, username, password_hash, role, totp_enabled) "
        "VALUES (gen_random_uuid(), 'legado.sem2fa', 'h', 'reader', false),"
        "       (gen_random_uuid(), 'legado.com2fa', 'h', 'admin', true)"
    )
    command.upgrade(config, "0008")
    rows = sql(
        "SELECT username, totp_enabled, activation_token_hash, activation_expires_at FROM users ORDER BY 1"
    )
    assert rows == [("legado.com2fa", True, None, None), ("legado.sem2fa", False, None, None)]

    command.downgrade(config, "0007")
    columns = sql(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'users' AND column_name LIKE 'activation%'"
    )
    assert columns == []
    assert sql("SELECT count(*) FROM users") == [(2,)]
    command.upgrade(config, "head")
    command.check(config)


# ----------------------------------------------------------------------------- relogio x janela do token


@pytest.mark.parametrize(("confirm_offset_seconds", "expected"), [(-1, 200), (0, 401), (1, 401)])
async def test_enrollment_confirmation_respects_the_exact_token_expiry(
    harness: Harness, confirm_offset_seconds: int, expected: int
) -> None:
    username = await harness.create_user("reader")
    hours = harness.settings.activation_token_hours
    harness.clock.advance(hours=hours, seconds=-60)
    client = harness.client(ip="192.0.2.130")
    step = await client.post("/auth/login", json={"username": username, "password": GOOD_PASSWORD})
    headers = {"X-CSRF-Token": step.json()["csrf_token"]}
    begin = await client.post(
        "/auth/2fa/enroll", json={"activation_token": harness.activation_tokens[username]}, headers=headers
    )
    assert begin.status_code == 200
    harness.totp_secrets[username] = begin.json()["secret"]
    harness.clock.advance(seconds=60 + confirm_offset_seconds)
    # codigo CORRETO para o instante: so a expiracao do token pode recusar
    done = await client.post(
        "/auth/2fa/enroll/confirm", json={"code": harness.code_for(username)}, headers=headers
    )
    assert done.status_code == expected, done.text
