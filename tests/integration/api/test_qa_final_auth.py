"""QA rodada FINAL, seguranca de auth: unlock por OUTRO processo do SO, Argon2 de custo padrao sob rajada,
lock por usuario (cancelamento, DoS cruzado) e throttle fail-closed."""

import asyncio
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.app import create_app
from api.binance_keys import FakeKeyVerifier
from api.security.throttle import FailureThrottle
from tests.api_support import GOOD_PASSWORD, FakeClock, make_settings
from tests.integration.api.conftest import Harness, Session

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[3]
BAD = "errada errada 123!"


def _real_cost_harness(sessions: async_sessionmaker[AsyncSession], **kw: object) -> Harness:
    clock = FakeClock()
    verifier = FakeKeyVerifier()
    settings = make_settings(argon2_time_cost=3, argon2_memory_kib=64 * 1024, argon2_parallelism=2, **kw)
    app = create_app(settings, sessions=sessions, verifier=verifier, clock=clock)
    return Harness(app=app, container=app.state.container, clock=clock, verifier=verifier, sessions=sessions)


async def test_os_process_cli_unlock_releases_the_running_api_within_5_seconds(
    scratch_sessions: async_sessionmaker[AsyncSession],
) -> None:
    clock = FakeClock()
    app = create_app(
        make_settings(unlock_recheck_seconds=5.0),
        sessions=scratch_sessions,
        verifier=FakeKeyVerifier(),
        clock=clock,
    )
    harness = Harness(
        app=app,
        container=app.state.container,
        clock=clock,
        verifier=FakeKeyVerifier(),
        sessions=scratch_sessions,
    )
    name = await harness.create_user("reader")
    ip = "192.0.2.77"
    attacker = harness.client(ip=ip)
    for _ in range(6):
        await attacker.post("/auth/login", json={"username": name, "password": BAD})
    assert (
        await attacker.post("/auth/login", json={"username": name, "password": GOOD_PASSWORD})
    ).status_code == 429

    database = scratch_sessions.kw["bind"].url.database
    assert database and database.endswith("_test")
    env = {
        **os.environ,
        "DATABASE_NAME": database,
        "API_ENVIRONMENT": "test",
        "API_SESSION_SECRET": "s" * 48,
        "API_MASTER_KEYS": make_settings().master_keys.get_secret_value(),
    }
    started = time.monotonic()
    done = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "api.cli", "unlock-user", "--username", name],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=120,
    )  # fmt: skip
    assert done.returncode == 0 and "desbloqueado" in done.stdout, (done.stdout, done.stderr[-500:])

    deadline = time.monotonic() + 5.5
    status = 429
    while time.monotonic() < deadline:
        r = await harness.client(ip=ip).post(
            "/auth/login", json={"username": name, "password": GOOD_PASSWORD}
        )
        status = r.status_code
        if status != 429:
            break
        await asyncio.sleep(0.25)
    assert status == 200, (
        f"continuou bloqueado {time.monotonic() - started:.1f}s apos o unlock por outro processo"
    )


async def test_cli_unlock_of_an_unknown_username_reports_failure_in_the_exit_code(
    scratch_sessions: async_sessionmaker[AsyncSession],
) -> None:
    database = scratch_sessions.kw["bind"].url.database
    assert database and database.endswith("_test")
    env = {
        **os.environ,
        "DATABASE_NAME": database,
        "API_ENVIRONMENT": "test",
        "API_SESSION_SECRET": "s" * 48,
        "API_MASTER_KEYS": make_settings().master_keys.get_secret_value(),
    }
    done = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "api.cli", "unlock-user", "--username", "ninguem.existe"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=120,
    )  # fmt: skip
    assert done.returncode != 0, "typo no nome: o operador acha que desbloqueou"


async def test_change_password_burst_with_default_cost_argon2_keeps_other_routes_responsive(
    scratch_sessions: async_sessionmaker[AsyncSession],
) -> None:
    harness = _real_cost_harness(scratch_sessions)
    victim = await harness.login(await harness.create_user("reader"))
    other = await harness.login(await harness.create_user("admin"))

    async def change(i: int) -> int:
        client = harness.client(ip=f"10.9.{i // 200}.{i % 200 + 1}")
        client.cookies.update(victim.client.cookies)
        r = await client.post(
            "/auth/password",
            json={"current_password": BAD, "new_password": "outra senha bem forte 77?"},
            headers=victim.headers,
        )
        await client.aclose()
        return r.status_code

    from sqlalchemy import text

    async def hold() -> None:
        async with scratch_sessions() as db:
            await db.execute(text("SELECT pg_sleep(0.5)"))

    await asyncio.gather(*(hold() for _ in range(12)))  # pool aquecido: separa handshake de contencao real
    latencies: list[float] = []
    stop = asyncio.Event()

    async def probe() -> None:
        while not stop.is_set():
            started = time.monotonic()
            r = await other.get("/auth/me")
            latencies.append(time.monotonic() - started)
            assert r.status_code == 200
            await asyncio.sleep(0.05)

    prober = asyncio.create_task(probe())
    codes = await asyncio.gather(*(change(i) for i in range(60)))
    stop.set()
    await prober
    assert set(codes) <= {401, 429}, sorted(set(codes))
    print(
        f"AUTH_ME lat={[round(x, 2) for x in latencies]} n={len(latencies)} mediana={statistics.median(latencies):.3f}s max={max(latencies):.3f}s codigos={ {c: codes.count(c) for c in set(codes)} }"
    )
    assert latencies and max(latencies) < 3.0, (max(latencies), statistics.median(latencies))
    assert (await other.get("/auth/me")).status_code == 200
    await victim.aclose()
    await other.aclose()


async def test_user_lock_is_released_on_cancellation_and_never_blocks_other_users(
    scratch_sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _real_cost_harness(scratch_sessions)
    ana = await harness.login(await harness.create_user("reader"))
    bia = await harness.login(await harness.create_user("reader"))
    original = harness.container.passwords.hash
    gate = asyncio.Event()

    async def stuck_hash(password: str) -> str:
        if not gate.is_set():
            await asyncio.sleep(30)
        return await original(password)

    monkeypatch.setattr(harness.container.passwords, "hash", stuck_hash)

    async def change(session: Session, new: str) -> int:
        r = await session.send(
            "POST", "/auth/password", json={"current_password": GOOD_PASSWORD, "new_password": new}
        )
        return r.status_code

    stuck = asyncio.create_task(change(ana, "uma senha nova bem forte 11?"))
    await asyncio.sleep(1.0)
    # Bia nao espera pela Ana: o lock e por usuario
    gate.set()
    assert await asyncio.wait_for(change(bia, "outra senha bem forte 22?"), 10) == 204
    stuck.cancel()
    await asyncio.gather(stuck, return_exceptions=True)
    # lock da Ana nao vazou: uma nova troca (com o hash liberado) termina
    assert await asyncio.wait_for(change(ana, "terceira senha bem forte 33?"), 10) == 204
    await ana.aclose()
    await bia.aclose()


def test_full_throttle_refuses_new_keys_but_keeps_known_keys_and_recovers_when_locks_expire() -> None:
    now = [0.0]
    throttle = FailureThrottle(max_keys=100, clock=lambda: now[0])
    for i in range(100):
        for _ in range(5):
            throttle.register_failure(f"k{i}")
    assert throttle.retry_after("novo") > 0, "fail-closed com mapa cheio de bloqueios ativos"
    assert throttle.retry_after("k3") > 0
    now[0] += 31.0
    assert throttle.retry_after("novo") == 0, "depois que os bloqueios expiram o login legitimo volta"
    throttle.register_failure("novo")
    assert len(throttle._failures) <= 100


async def test_same_user_burst_does_not_hold_pool_connections_while_waiting_for_the_row_lock(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mata o mutante 'sem lock por usuario': sem ele cada requisicao espera o FOR UPDATE segurando uma conexao
    e uma rota de OUTRO usuario fica na fila do pool."""
    from api.services.auth import AuthService

    session = await harness.login(await harness.create_user("admin"))
    other = await harness.login(await harness.create_user("reader"))
    original = AuthService._replace_recovery_codes

    async def slow(self: AuthService, db: AsyncSession, user: object) -> list[str]:
        await asyncio.sleep(0.2)
        return await original(self, db, user)  # type: ignore[arg-type]

    monkeypatch.setattr(AuthService, "_replace_recovery_codes", slow)

    async def regenerate() -> int:
        r = await session.send("POST", "/auth/2fa/recovery-codes")
        return r.status_code

    burst = asyncio.gather(*(regenerate() for _ in range(40)))
    await asyncio.sleep(1.0)
    started = time.monotonic()
    me = await other.get("/auth/me")
    waited = time.monotonic() - started
    codes = await burst
    assert me.status_code == 200
    assert set(codes) == {200}, sorted(set(codes))
    assert waited < 1.5, (
        f"rota de outro usuario esperou {waited:.1f}s: pool preso pela rajada do mesmo usuario"
    )
    await session.aclose()
    await other.aclose()
