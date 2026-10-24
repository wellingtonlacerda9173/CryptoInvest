"""Backtest pela API: job em subprocesso (fora do event loop), progresso em tabela, relatorio, cancelamento,
limites de tempo e memoria, recuperacao apos reinicio. Dados sinteticos: nenhuma rede, nenhum banco de velas."""

import asyncio
import time
import uuid
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from api.services.backtests import BacktestRunner
from engine.domain.types import Timeframe
from engine.infrastructure.persistence.models import BacktestJobRow
from tests.integration.api.conftest import Harness, Session
from tests.integration.api.runner_support import (
    build_harness,
    fake_link,
    module_db_url,
    module_sessions,
    rh_admin,
    rh_reader,
)
from tests.redis_fakes import FakeLink
from tests.synthetic import aligned_start, write_synthetic_cache

pytestmark = pytest.mark.integration

SYMBOL = "BTC/USDT"


@pytest.fixture(scope="module")
def cache_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("candles")
    write_synthetic_cache(path, SYMBOL, Timeframe.H4, aligned_start(400, Timeframe.H4), 400)
    return path


@pytest.fixture
def bt(module_sessions: Any, fake_link: FakeLink, cache_dir: Path) -> Harness:
    return build_harness(
        module_sessions,
        fake_link,
        backtest_cache_dir=cache_dir,
        backtest_use_db=False,
        backtest_timeout_seconds=300,
    )


@pytest.fixture
async def bt_admin(bt: Harness) -> Any:
    session = await bt.login(await bt.create_user("admin"))
    yield session
    await session.aclose()


@pytest.fixture
async def bt_reader(bt: Harness) -> Any:
    session = await bt.login(await bt.create_user("reader"))
    yield session
    await session.aclose()


def body(**overrides: Any) -> dict[str, Any]:
    return {"symbols": [SYMBOL], "timeframe": "4h", "years": 0.25, **overrides}


async def wait_for(
    session: Session, job_id: str, done: Callable[[dict[str, Any]], bool], limit: float = 240
) -> dict[str, Any]:
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        job = (await session.get(f"/backtests/{job_id}")).json()
        if done(job):
            return job  # type: ignore[no-any-return]
        await asyncio.sleep(0.4)
    raise AssertionError(f"tempo esgotado esperando o backtest: {job}")


# --------------------------------------------------------------------------- acesso e validacao


async def test_only_the_admin_creates_and_cancels_and_everyone_signed_in_reads(
    bt: Harness, bt_admin: Session, bt_reader: Session
) -> None:
    assert (await bt_reader.send("POST", "/backtests", json=body())).status_code == 403
    assert (await bt_reader.send("POST", f"/backtests/{'0' * 32}/cancel")).status_code == 403
    anonymous = bt.client()
    assert (await anonymous.get("/backtests")).status_code == 401
    await anonymous.aclose()
    assert (await bt_reader.get("/backtests")).status_code == 200
    assert (await bt_reader.get(f"/backtests/{'0' * 32}")).status_code == 404
    assert (await bt_reader.get("/backtests/nao-e-um-id")).status_code == 422
    assert (await bt_admin.send("POST", f"/backtests/{'0' * 32}/cancel")).status_code == 404


async def test_invalid_requests_are_rejected_before_anything_runs(bt: Harness, bt_admin: Session) -> None:
    cases = [
        ({"symbols": []}, "symbols"),
        ({"years": 9}, "máximo"),
        ({"years": 0.1, "walk_forward": True}, "curto demais"),
        ({"years": 3, "walk_forward": True, "grid": "min_confidence=0.5,0.55,0.6,0.65,0.7;rsi_min=40,45,50,55;reward_risk=2,3"}, "Combinações demais"),
        ({"years": 3, "walk_forward": True, "grid": "xxx"}, "Grade"),
        ({"start": "2026-01-01T00:00:00Z"}, "start"),
        ({"params": {"magico": "1"}}, "desconhecidos"),
    ]  # fmt: skip
    for extra, fragment in cases:
        response = await bt_admin.send("POST", "/backtests", json=body(**extra))
        assert response.status_code == 422, (extra, response.text)
        assert fragment in response.text, (extra, response.text)
    assert (await bt_admin.get("/backtests")).json()["items"] == []


# --------------------------------------------------------------------------- execucao completa


async def test_a_job_runs_in_a_subprocess_and_produces_the_full_report(
    bt: Harness, bt_admin: Session, bt_reader: Session
) -> None:
    started = time.monotonic()
    created = await bt_admin.send(
        "POST", "/backtests", json=body(years=0.5, taker_fee="0.002", capital="20000")
    )
    job = created.json()
    assert (
        created.status_code == 202
        and job["status"] in {"queued", "running"}
        and job["created_by"] == bt_admin.username
    )
    assert (
        job["params"]["taker_fee"] == "0.002" and job["params"]["days"] == 182 and job["has_report"] is False
    )
    assert time.monotonic() - started < 5, "a criacao nao pode esperar o backtest"

    early = (await bt_admin.get(f"/backtests/{job['id']}/report")).status_code
    assert early in {200, 409}
    final = await wait_for(
        bt_reader, job["id"], lambda j: j["status"] in {"succeeded", "failed", "cancelled"}
    )
    assert final["status"] == "succeeded", final
    assert (
        final["progress"]["percent"] == 100
        and final["progress"]["stage"] == "done"
        and final["has_report"] is True
    )
    assert final["started_at"] and final["finished_at"] and final["error"] is None

    report = (await bt_reader.get(f"/backtests/{job['id']}/report")).json()
    assert (
        report["mode"] == "backtest"
        and report["disclaimer"] == "Resultado de simulação no passado. Não garante o futuro."
    )
    assert report["assumptions"]["taker_fee"] == "0.002" and report["assumptions"]["capital"] == "20000"
    [result] = report["results"]
    assert (
        result["symbol"] == SYMBOL
        and result["candles"] > 500
        and result["data_source"].startswith("cache local")
    )
    assert {
        "max_drawdown",
        "total_return",
        "sharpe",
        "win_rate",
        "profit_factor",
        "monthly",
        "by_strategy",
    } <= set(result["metrics"])
    assert result["equity_curve"] and result["trades_total"] == result["metrics"]["trades"]
    assert "by_code" in result["avoided"] and result["walk_forward"] is None

    listing = (await bt_reader.get("/backtests", params={"limit": 5})).json()
    assert job["id"] in {j["id"] for j in listing["items"]}
    audit = (await bt_admin.get("/audit", params={"action": "backtest."})).json()["items"]
    assert audit[-1]["action"] == "backtest.created" and audit[-1]["actor"] == bt_admin.username


async def test_walk_forward_job_reports_windows_and_a_plain_verdict(bt: Harness, bt_admin: Session) -> None:
    job = (
        await bt_admin.send(
            "POST",
            "/backtests",
            json=body(
                years=1.0,
                walk_forward=True,
                train_days=120,
                test_days=45,
                grid="min_confidence=0.5,0.6;rsi_min=45,50",
            ),
        )
    ).json()
    final = await wait_for(bt_admin, job["id"], lambda j: j["status"] in {"succeeded", "failed", "cancelled"})
    assert final["status"] == "succeeded", final
    wf = (await bt_admin.get(f"/backtests/{job['id']}/report")).json()["results"][0]["walk_forward"]
    assert len(wf["windows"]) >= 2 and wf["verdict_text"] and wf["assessment"]["verdict"]
    assert wf["config"]["train_days"] == 120 and set(wf["grid"]) == {"min_confidence", "rsi_min"}


# --------------------------------------------------------------------------- cancelar, fila e limites


async def slow_job(session: Session, **extra: Any) -> dict[str, Any]:
    return (  # type: ignore[no-any-return]
        await session.send(
            "POST",
            "/backtests",
            json=body(years=1.0, walk_forward=True, train_days=60, test_days=20, **extra),
        )
    ).json()


async def test_a_running_job_can_be_cancelled_and_the_process_dies(bt: Harness, bt_admin: Session) -> None:
    job = await slow_job(bt_admin, grid="min_confidence=0.5,0.55,0.6,0.65;rsi_min=40,45,50,55")
    await wait_for(bt_admin, job["id"], lambda j: j["status"] == "running", 60)
    runner: BacktestRunner = bt.container.backtests  # type: ignore[assignment]
    cancelled = (await bt_admin.send("POST", f"/backtests/{job['id']}/cancel")).json()
    assert cancelled["cancel_requested"] is True
    final = await wait_for(
        bt_admin, job["id"], lambda j: j["status"] in {"cancelled", "succeeded", "failed"}, 60
    )
    assert final["status"] == "cancelled" and final["finished_at"] and not final["has_report"]
    for _ in range(50):
        if not runner._procs:
            break
        await asyncio.sleep(0.1)
    assert runner._procs == {}
    again = await bt_admin.send("POST", f"/backtests/{job['id']}/cancel")
    assert again.status_code == 409
    assert (await bt_admin.get(f"/backtests/{job['id']}/report")).status_code == 409


async def test_concurrency_is_limited_and_a_queued_job_can_be_cancelled_without_running(
    bt: Harness, bt_admin: Session
) -> None:
    first = await slow_job(bt_admin, grid="min_confidence=0.5,0.55,0.6,0.65;rsi_min=40,45,50,55")
    second = await slow_job(bt_admin)
    await wait_for(bt_admin, first["id"], lambda j: j["status"] == "running", 60)
    assert (await bt_admin.get(f"/backtests/{second['id']}")).json()["status"] == "queued"
    await bt_admin.send("POST", f"/backtests/{second['id']}/cancel")
    await bt_admin.send("POST", f"/backtests/{first['id']}/cancel")
    final = await wait_for(
        bt_admin, second["id"], lambda j: j["status"] in {"cancelled", "failed", "succeeded"}, 90
    )
    assert final["status"] == "cancelled" and final["started_at"] is None
    await wait_for(bt_admin, first["id"], lambda j: j["status"] in {"cancelled", "failed", "succeeded"}, 90)


async def test_time_limit_kills_the_job(module_sessions: Any, fake_link: FakeLink, cache_dir: Path) -> None:
    harness = build_harness(
        module_sessions,
        fake_link,
        backtest_cache_dir=cache_dir,
        backtest_use_db=False,
        backtest_timeout_seconds=2,
    )
    admin = await harness.login(await harness.create_user("admin"))
    job = await slow_job(admin, grid="min_confidence=0.5,0.55,0.6,0.65;rsi_min=40,45,50,55")
    final = await wait_for(
        admin, job["id"], lambda j: j["status"] in {"failed", "succeeded", "cancelled"}, 60
    )
    assert final["status"] == "failed" and "Tempo máximo excedido" in final["error"]
    await admin.aclose()


async def test_memory_limit_stops_the_job_with_a_plain_message(
    module_sessions: Any, fake_link: FakeLink, cache_dir: Path
) -> None:
    harness = build_harness(
        module_sessions,
        fake_link,
        backtest_cache_dir=cache_dir,
        backtest_use_db=False,
        backtest_max_memory_mb=64,
    )
    admin = await harness.login(await harness.create_user("admin"))
    job = await slow_job(admin, grid="min_confidence=0.5,0.55,0.6,0.65;rsi_min=40,45,50,55")
    final = await wait_for(
        admin, job["id"], lambda j: j["status"] in {"failed", "succeeded", "cancelled"}, 90
    )
    assert final["status"] == "failed" and "Memória máxima excedida" in final["error"]
    await admin.aclose()


async def test_a_failing_job_reports_a_plain_error_without_traceback(bt: Harness, bt_admin: Session) -> None:
    async with bt.sessions.begin() as db:
        db.add(
            BacktestJobRow(
                id="c" * 32, created_by="x", status="queued", params={"timeframe": "7y"}, progress={},
                cancel_requested=False, created_at=bt.clock(),
            )
        )  # fmt: skip
    bt.container.backtests.submit("c" * 32)  # type: ignore[union-attr]
    final = await wait_for(
        bt_admin, "c" * 32, lambda j: j["status"] in {"failed", "succeeded", "cancelled"}, 60
    )
    assert final["status"] == "failed" and final["error"] and "Traceback" not in final["error"]
    assert not final["has_report"]


# --------------------------------------------------------------------------- recuperacao


async def test_after_an_api_restart_running_jobs_fail_and_queued_jobs_resume(
    bt: Harness, bt_admin: Session, cache_dir: Path
) -> None:
    now = bt.clock()
    params = {
        "symbols": [SYMBOL], "timeframe": "4h", "start": (now - timedelta(days=60)).isoformat(), "end": now.isoformat(),
        "strategies": ["elephant", "ema"], "params": {}, "capital": "10000", "taker_fee": "0.001", "maker_fee": "0.001",
        "slippage_bps": "5", "latency_candles": 1, "walk_forward": False, "train_days": 180, "test_days": 60, "grid": None, "days": 60,
    }  # fmt: skip
    from engine.runner.candle_source import floor_to_timeframe

    params["end"] = floor_to_timeframe(now, Timeframe.H4).isoformat()
    params["start"] = (floor_to_timeframe(now, Timeframe.H4) - timedelta(days=60)).isoformat()
    async with bt.sessions.begin() as db:
        for job_id, status in (("a" * 32, "running"), ("b" * 32, "queued")):
            db.add(
                BacktestJobRow(
                    id=job_id,
                    created_by="x",
                    status=status,
                    params=params,
                    progress={},
                    cancel_requested=False,
                    created_at=now,
                )
            )
    runner: BacktestRunner = bt.container.backtests  # type: ignore[assignment]
    await runner.recover()
    interrupted = (await bt_admin.get(f"/backtests/{'a' * 32}")).json()
    assert interrupted["status"] == "failed" and "API reiniciou" in interrupted["error"]
    resumed = await wait_for(
        bt_admin, "b" * 32, lambda j: j["status"] in {"succeeded", "failed", "cancelled"}
    )
    assert resumed["status"] == "succeeded", resumed


# --------------------------------------------------------------------------- endurecimento


@pytest.mark.parametrize(
    "extra",
    [
        {"strategies": ["elephant", "elephant"]},
        {"params": {"min_confidence": "5"}},
        {"params": {"min_confidence": "-0.1"}},
        {"params": {"rsi_min": "100"}},
        {"params": {"reward_risk": "1.5"}},
        {"params": {"reward_risk": "NaN"}},
        {"params": {"reward_risk": "Infinity"}},
        {"start": "2010-01-01T00:00:00Z", "end": "2011-01-01T00:00:00Z"},
        {"years": 7},
        {"years": 1e30},
        {"nao_existe": 1},
    ],
)
async def test_params_are_bounded_and_duplicates_and_ancient_periods_are_422(
    bt: Harness, bt_admin: Session, extra: dict[str, Any]
) -> None:
    response = await bt_admin.send("POST", "/backtests", json=body(**extra))
    assert response.status_code == 422, (extra, response.text)


async def test_recover_does_not_fail_a_job_whose_owner_is_still_alive(bt: Harness, bt_admin: Session) -> None:
    now = bt.clock()
    alive_id, dead_id = uuid.uuid4().hex, uuid.uuid4().hex
    beats = {
        alive_id: {"owner": "outro-processo", "owner_beat": now.isoformat()},
        dead_id: {"owner": "outro-processo", "owner_beat": (now - timedelta(minutes=5)).isoformat()},
    }
    async with bt.sessions.begin() as db:
        for job_id, progress in beats.items():
            db.add(
                BacktestJobRow(
                    id=job_id,
                    created_by="x",
                    status="running",
                    params={},
                    progress=progress,
                    cancel_requested=False,
                    created_at=now,
                )
            )
    runner: BacktestRunner = bt.container.backtests  # type: ignore[assignment]
    await runner.recover()
    assert (await bt_admin.get(f"/backtests/{alive_id}")).json()["status"] == "running"
    dead = (await bt_admin.get(f"/backtests/{dead_id}")).json()
    assert dead["status"] == "failed" and "API reiniciou" in dead["error"]
    assert "owner" not in str(dead["progress"])


async def _plant_running(bt: Harness, *, owner_beat: Any) -> str:
    job_id = uuid.uuid4().hex
    progress = {} if owner_beat is None else {"owner": "api-que-caiu", "owner_beat": owner_beat.isoformat()}
    async with bt.sessions.begin() as db:
        db.add(
            BacktestJobRow(
                id=job_id,
                created_by="x",
                status="running",
                params={},
                progress=progress,
                cancel_requested=False,
                created_at=bt.clock(),
            )
        )
    return job_id


async def test_a_zombie_job_after_a_quick_restart_is_failed_by_the_sweep_once_its_beat_expires(
    bt: Harness, bt_admin: Session
) -> None:
    runner: BacktestRunner = bt.container.backtests  # type: ignore[assignment]
    job_id = await _plant_running(bt, owner_beat=bt.clock() - timedelta(seconds=5))
    await runner.recover()  # reinicio < 45 s: o dono antigo ainda parece vivo
    assert (await bt_admin.get(f"/backtests/{job_id}")).json()["status"] == "running"
    await runner.sweep()
    assert (await bt_admin.get(f"/backtests/{job_id}")).json()["status"] == "running"

    bt.clock.advance(seconds=60)
    assert await runner.sweep() >= 1

    final = (await bt_admin.get(f"/backtests/{job_id}")).json()
    assert final["status"] == "failed" and "API reiniciou" in final["error"]


async def test_the_sweep_never_touches_a_job_this_process_is_running_and_fails_one_without_any_beat(
    bt: Harness, bt_admin: Session
) -> None:
    runner: BacktestRunner = bt.container.backtests  # type: ignore[assignment]
    mine = await _plant_running(bt, owner_beat=bt.clock() - timedelta(minutes=10))
    orphan = await _plant_running(bt, owner_beat=None)
    runner._tasks[mine] = asyncio.get_running_loop().create_future()  # type: ignore[assignment]
    try:
        await runner.sweep()
    finally:
        runner._tasks.pop(mine)

    assert (await bt_admin.get(f"/backtests/{mine}")).json()["status"] == "running"
    assert (await bt_admin.get(f"/backtests/{orphan}")).json()["status"] == "failed"


async def test_the_background_sweeper_loop_resolves_a_zombie_on_its_own(
    bt: Harness, bt_admin: Session
) -> None:
    runner: BacktestRunner = bt.container.backtests  # type: ignore[assignment]
    job_id = await _plant_running(bt, owner_beat=bt.clock() - timedelta(minutes=5))
    task = asyncio.create_task(runner.run_sweeper(interval=0.05))
    try:
        for _ in range(60):
            if (await bt_admin.get(f"/backtests/{job_id}")).json()["status"] == "failed":
                break
            await asyncio.sleep(0.1)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert (await bt_admin.get(f"/backtests/{job_id}")).json()["status"] == "failed"
