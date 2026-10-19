import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from engine.application.backtest import BacktestEngine, BacktestSettings, compute_metrics
from engine.domain.types import Timeframe
from engine.runner.backtest import build_strategies, main
from engine.runner.candle_source import floor_to_timeframe, load_candles

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("binance_reachable")]

SYMBOL = "BTC/USDT"
DAYS = 40


def _period() -> tuple[datetime, datetime]:
    end = floor_to_timeframe(datetime.now(UTC), Timeframe.H1) - timedelta(days=1)
    return end - timedelta(days=DAYS), end


async def test_real_candles_run_the_full_pipeline_deterministically(tmp_path: Path) -> None:
    start, end = _period()
    loaded = await load_candles(SYMBOL, Timeframe.H1, start, end, cache_dir=tmp_path, use_db=False)
    candles = loaded.candles
    assert len(candles) >= DAYS * 24 * 0.95
    assert "Binance" in loaded.source

    again = await load_candles(SYMBOL, Timeframe.H1, start, end, cache_dir=tmp_path, use_db=False)
    assert again.candles == candles
    assert "cache local" in again.source
    assert "Binance" not in again.source

    settings = BacktestSettings(slippage_bps=Decimal(5))
    fixed = datetime(2030, 1, 1, tzinfo=UTC)
    engine = BacktestEngine(build_strategies(["elephant", "ema"], {}), settings, clock=lambda: fixed)
    first = await engine.run(candles)
    second = await engine.run(candles)

    assert first == second
    assert len(first.equity_curve) == len(candles)
    assert first.final_realized_equity == settings.initial_capital + sum(
        (t.pnl for t in first.trades), Decimal(0)
    )
    metrics = compute_metrics(first)
    assert 0 <= metrics.max_drawdown <= 1
    assert metrics.trades == len(first.round_trips)


@pytest.mark.usefixtures("db_ready")
async def test_candles_persist_to_timescale_and_reload_from_it(tmp_path: Path) -> None:
    start, end = _period()
    first = await load_candles(SYMBOL, Timeframe.H1, start, end, cache_dir=tmp_path / "a")
    second = await load_candles(SYMBOL, Timeframe.H1, start, end, cache_dir=tmp_path / "b")

    assert "timescaledb" in second.source
    assert second.candles == first.candles


def test_cli_end_to_end_writes_json_markdown_and_csv_reports(tmp_path: Path) -> None:
    main(
        [
            "--symbol",
            SYMBOL,
            "--years",
            "0.12",
            "--strategies",
            "elephant,ema",
            "--walk-forward",
            "--train-days",
            "21",
            "--test-days",
            "7",
            "--grid",
            "min_confidence=0.5,0.6;reward_risk=2,3",
            "--min-train-trades",
            "1",
            "--no-db",
            "--out",
            str(tmp_path / "reports"),
            "--cache-dir",
            str(tmp_path / "cache"),
        ]
    )

    folder = tmp_path / "reports" / "BTCUSDT_1h_elephant-ema"
    names = {p.name for p in folder.iterdir()}
    assert {"report.json", "report.md", "trades.csv", "equity.csv", "decisions.csv"} <= names
    assert {"walk_forward.json", "walk_forward.md", "oos_equity.csv"} <= names
    report = json.loads((folder / "report.json").read_text(encoding="utf-8"))
    assert report["meta"]["data_source"]
    assert report["metrics"]["max_drawdown"] >= 0
    assert (folder / "report.md").read_text(encoding="utf-8").startswith("# Backtest BTC/USDT 1h")
