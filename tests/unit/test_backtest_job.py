"""Worker de backtest do painel: protocolo, relatorio sintetico, walk-forward e limite de memoria."""

import json
import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from engine.domain.types import Timeframe
from engine.runner import backtest_job
from engine.runner.backtest_job import downsample, main, rss_mb, run_job, start_memory_guard
from tests.synthetic import write_synthetic_cache

FIXED_END = datetime(2026, 6, 1, tzinfo=UTC)


def params(days: int, **overrides: Any) -> dict[str, Any]:
    start = FIXED_END - timedelta(days=days)
    base: dict[str, Any] = {
        "symbols": ["BTC/USDT"],
        "timeframe": "4h",
        "start": start.isoformat(),
        "end": FIXED_END.isoformat(),
        "strategies": ["elephant", "ema"],
        "params": {},
        "capital": "10000",
        "taker_fee": "0.001",
        "maker_fee": "0.001",
        "slippage_bps": "5",
        "latency_candles": 1,
        "walk_forward": False,
        "train_days": 15,
        "test_days": 7,
        "grid": None,
    }
    return base | overrides


def test_downsample_keeps_the_last_point_and_bounds_the_size() -> None:
    assert downsample(list(range(10)), 20) == list(range(10))
    sampled = downsample(list(range(1000)), 100)
    assert len(sampled) == 100 and sampled[0] == 0 and sampled[-1] == 999


def test_rss_is_a_positive_number_of_megabytes() -> None:
    assert 5 < rss_mb() < 100_000


def test_memory_guard_reports_and_kills_the_process_over_the_limit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    exited = threading.Event()
    codes: list[int] = []

    def fake_exit(code: int) -> None:
        codes.append(code)
        exited.set()

    readings = iter([500.0])
    monkeypatch.setattr(os, "_exit", fake_exit)
    start_memory_guard(100.0, interval=0.01, measure=lambda: next(readings, 0.0))
    assert exited.wait(2)
    assert codes == [backtest_job.EXIT_MEMORY]
    assert '"memory_limit"' in capsys.readouterr().out


async def test_run_job_builds_the_report_with_synthetic_data(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_synthetic_cache(tmp_path, "BTC/USDT", Timeframe.H4, FIXED_END - timedelta(days=60), 60)
    report = await run_job(params(60), cache_dir=tmp_path, use_db=False)

    [result] = report["results"]
    assert result["symbol"] == "BTC/USDT" and result["candles"] == 60 * 6
    assert result["data_source"].startswith("cache local")
    metrics = result["metrics"]
    assert {
        "max_drawdown",
        "total_return",
        "trades",
        "win_rate",
        "profit_factor",
        "sharpe",
        "idle_pct",
    } <= set(metrics)
    assert 0 < len(result["equity_curve"]) <= 400
    assert result["trades_total"] == metrics["trades"] and result["walk_forward"] is None
    assert "signals" in result["avoided"] and "by_code" in result["avoided"]
    assert report["assumptions"]["taker_fee"] == "0.001" and report["assumptions"]["latency_candles"] == 1
    progress = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert progress[0]["stage"] == "loading" and progress[-1]["percent"] == 100.0
    json.dumps(report)


async def test_run_job_with_walk_forward_returns_windows_and_a_verdict(tmp_path: Path) -> None:
    write_synthetic_cache(tmp_path, "BTC/USDT", Timeframe.H4, FIXED_END - timedelta(days=50), 50)
    grid = "min_confidence=0.5,0.6;rsi_min=45,50"
    report = await run_job(params(50, walk_forward=True, grid=grid), cache_dir=tmp_path, use_db=False)

    wf = report["results"][0]["walk_forward"]
    assert len(wf["windows"]) >= 2 and wf["config"]["train_days"] == 15
    assert wf["assessment"]["verdict"] and wf["verdict_text"]
    assert {"max_drawdown", "trades"} <= set(wf["out_of_sample"])
    assert set(wf["grid"]) == {"min_confidence", "rsi_min"}


async def test_walk_forward_with_a_short_period_reports_the_engine_error(tmp_path: Path) -> None:
    write_synthetic_cache(tmp_path, "BTC/USDT", Timeframe.H4, FIXED_END - timedelta(days=15), 15)
    report = await run_job(
        params(15, walk_forward=True, train_days=20, test_days=10), cache_dir=tmp_path, use_db=False
    )
    assert "periodo curto demais" in report["results"][0]["walk_forward"]["error"]


def test_main_writes_the_report_file_and_signals_done(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cache = tmp_path / "cache"
    write_synthetic_cache(cache, "BTC/USDT", Timeframe.H4, FIXED_END - timedelta(days=30), 30)
    job, out = tmp_path / "job.json", tmp_path / "out.json"
    job.write_text(json.dumps(params(30)), encoding="utf-8")
    code = main(["--params", str(job), "--out", str(out), "--cache-dir", str(cache), "--no-db"])
    assert code == 0 and json.loads(out.read_text(encoding="utf-8"))["results"]
    assert json.loads(capsys.readouterr().out.splitlines()[-1]) == {"type": "done"}


def test_main_reports_a_failure_without_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    job = tmp_path / "job.json"
    job.write_text(json.dumps(params(30, timeframe="7y")), encoding="utf-8")
    code = main(
        ["--params", str(job), "--out", str(tmp_path / "o.json"), "--cache-dir", str(tmp_path), "--no-db"]
    )
    last = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert code == backtest_job.EXIT_FAILED and last["type"] == "error" and "Traceback" not in last["message"]
