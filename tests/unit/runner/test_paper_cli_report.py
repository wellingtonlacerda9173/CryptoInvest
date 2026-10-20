"""CLI, fila de comandos, configuracao e relatorios do paper (sem rede nem banco)."""

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from engine.application.backtest.models import EquityPoint
from engine.domain.journal import ControlAction, ControlEvent
from engine.domain.risk import RiskConfig
from engine.domain.trade import ExitReason, Trade
from engine.domain.types import Timeframe
from engine.runner import paper
from engine.runner.paper_config import PaperRunConfig, RunPaths, read_latest, validate_run_id, write_latest
from engine.runner.paper_control import Command, CommandName, CommandQueue, read_status, write_status
from engine.runner.paper_report import (
    BacktestSide,
    DecisionSummary,
    ReportInputs,
    build_cumulative,
    build_daily,
    to_markdown,
    write_report,
)

D = Decimal
T0 = datetime(2026, 10, 19, 12, tzinfo=UTC)


def command(name: CommandName = CommandName.PAUSE, **kw: object) -> Command:
    values: dict[str, object] = {"name": name, "operator": "ana", "reason": "teste", "issued_at": T0} | kw
    return Command.model_validate(values)


def test_command_queue_is_ordered_atomic_and_archives(tmp_path: Path) -> None:
    queue = CommandQueue(tmp_path / "commands")
    queue.send(command(CommandName.PAUSE, issued_at=T0))
    queue.send(command(CommandName.STOP, issued_at=T0 + timedelta(seconds=1)))

    pending = queue.pending()
    assert [c.name for _, c in pending] == [CommandName.PAUSE, CommandName.STOP]
    queue.done(pending[0][0])
    assert [c.name for _, c in queue.pending()] == [CommandName.STOP]
    assert (tmp_path / "commands" / "processed").is_dir()


def test_invalid_command_file_is_rejected_not_executed(tmp_path: Path) -> None:
    (tmp_path / "20261019T120000000000-resume.json").write_text('{"name": "resume", "operator": " "}')
    queue = CommandQueue(tmp_path)

    assert queue.pending() == []
    assert (tmp_path / "rejected").is_dir()


def test_manual_commands_require_operator_and_reason() -> None:
    with pytest.raises(ValidationError):
        command(CommandName.REACTIVATE, operator="  ")
    with pytest.raises(ValidationError):
        command(CommandName.REACTIVATE, reason="")


def test_status_file_roundtrip(tmp_path: Path) -> None:
    write_status(tmp_path / "s" / "status.json", {"state": "RUNNING", "n": D(1)})

    assert read_status(tmp_path / "s" / "status.json") == {"state": "RUNNING", "n": "1"}
    assert read_status(tmp_path / "missing.json") is None


def test_run_id_validation_and_latest_pointer(tmp_path: Path) -> None:
    assert validate_run_id("paper-20261019-1200") == "paper-20261019-1200"
    for bad in ("", "../x", "a b", "x" * 65):
        with pytest.raises(ValueError, match="run_id invalido"):
            validate_run_id(bad)
    write_latest(tmp_path, "r1")
    assert read_latest(tmp_path) == "r1"
    assert RunPaths(tmp_path, "r1").commands == tmp_path / "r1" / "commands"


def test_config_defaults_and_hash() -> None:
    config = PaperRunConfig()

    assert config.symbols == ("BTC/USDT", "ETH/USDT")
    assert config.timeframe is Timeframe.H1
    assert config.capital == D(10_000)
    assert config.allow_open_calendar is False
    assert config.eval_lag_seconds == 120
    assert config.gateway_config().initial_quote == D(10_000)
    assert config.config_hash() == PaperRunConfig().config_hash()
    with pytest.raises(ValidationError):
        PaperRunConfig(symbols=("BTC/USDT", "BTC/USDT"))


def test_start_parser_defaults_are_conservative() -> None:
    args = paper.build_parser().parse_args(["start"])

    assert args.allow_open_calendar is False
    assert args.ws_url is None
    assert args.symbols == "BTC/USDT,ETH/USDT"
    config = paper.build_config(args)
    assert config.allow_open_calendar is False
    assert config.reconcile_seconds == 300
    assert paper.DEFAULT_CALENDAR.as_posix() == "config/volatility_events.json"


def test_reactivate_and_resume_demand_operator_and_reason(capsys: pytest.CaptureFixture[str]) -> None:
    for name in ("reactivate", "resume"):
        with pytest.raises(SystemExit) as error:
            paper.build_parser().parse_args([name])
        assert error.value.code == 2
    capsys.readouterr()


def test_control_commands_are_queued_for_the_running_robot(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    RunPaths(tmp_path, "r1").ensure()
    args = paper.build_parser().parse_args(
        [
            "emergency-stop",
            "--run-id",
            "r1",
            "--data-dir",
            str(tmp_path),
            "--operator",
            "ana",
            "--reason",
            "incidente",
            "--liquidate",
        ]
    )

    assert paper.run_command(args) == paper.EXIT_OK
    [(_, sent)] = CommandQueue(RunPaths(tmp_path, "r1").commands).pending()
    assert sent.name is CommandName.EMERGENCY_STOP
    assert sent.liquidate is True
    assert sent.reason == "incidente"
    assert "nao parece estar em execucao" in capsys.readouterr().out


def test_unknown_run_is_a_usage_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    args = paper.build_parser().parse_args(["stop", "--run-id", "nope", "--data-dir", str(tmp_path)])

    assert paper.run_command(args) == paper.EXIT_USAGE
    assert "desconhecido" in capsys.readouterr().err


# ---------------------------------------------------------------- relatorio


def trade(trade_id: str, *, symbol: str = "BTC/USDT", exit_price: str = "110", day: int = 19) -> Trade:
    opened = datetime(2026, 10, day, 1, tzinfo=UTC)
    return Trade(
        trade_id=trade_id,
        symbol=symbol,
        quantity=D(1),
        entry_price=D(100),
        exit_price=D(exit_price),
        opened_at=opened,
        closed_at=opened + timedelta(hours=3),
        exit_reason=ExitReason.TAKE_PROFIT,
    )


def inputs(trades: list[Trade], **kw: object) -> ReportInputs:
    start = datetime(2026, 10, 18, tzinfo=UTC)
    now = datetime(2026, 10, 19, 12, tzinfo=UTC)
    curve = [
        EquityPoint(
            at=start + timedelta(minutes=30 * i), equity=D(10_000), realized=D(10_000), in_position=False
        )
        for i in range(60)
    ]
    values: dict[str, object] = {
        "run_id": "r1",
        "capital": D(10_000),
        "risk": RiskConfig(),
        "timeframe": Timeframe.H1,
        "symbols": ("BTC/USDT", "ETH/USDT"),
        "strategies": ("elephant", "ema"),
        "trades": trades,
        "curve": curve,
        "events": [ControlEvent(at=start, action=ControlAction.START, operator="system", reason="inicio")],
        "decisions": [
            DecisionSummary(start + timedelta(hours=i), "BTC/USDT", "no_signal", False) for i in range(5)
        ],
        "now": now,
    }
    return ReportInputs(**(values | kw))  # type: ignore[arg-type]


def test_report_without_trades_is_honest_that_staying_in_usdt_is_valid() -> None:
    report = build_cumulative(inputs([]))

    assert report["metrics"]["trades"] == 0
    assert report["final_equity"] == "10000"
    assert any("parado em USDT" in n or "Ficar parado em USDT" in n for n in report["notes"])
    assert report["live_gate"]["approved"] is False
    assert report["decisions"]["by_code"] == {"no_signal": 5}
    assert "Ficar parado em USDT" in to_markdown(report)


def test_open_calendar_run_is_flagged_as_not_evidence_for_live() -> None:
    report = build_cumulative(inputs([], allow_open_calendar=True))

    assert any("allow-open-calendar" in n for n in report["notes"])


def test_daily_report_isolates_the_day_and_chains_the_starting_capital() -> None:
    trades = [trade("a", day=18), trade("b", exit_price="95")]
    report = build_daily(inputs(trades), date(2026, 10, 19))

    assert report["scope"] == "daily"
    assert report["initial_equity"] == "10010"
    assert [t["trade_id"] for t in report["trades"]] == ["b"]
    assert report["metrics"]["trades"] == 1
    assert report["metrics"]["losing_trades"] == 1
    assert report["complete_day"] is False


def test_cumulative_report_compares_with_the_backtest_and_lists_gate_criteria() -> None:
    paper_trades = [trade("a")]
    backtest = BacktestSide("BTC/USDT", trades=(trade("bt"),), candles=48)
    skipped = BacktestSide("ETH/USDT", note="periodo menor que uma vela fechada do timeframe")
    report = build_cumulative(inputs(paper_trades, backtests=[backtest, skipped]))

    [btc, eth] = report["backtest_comparison"]
    assert btc["comparison"]["matched"] == 1
    assert btc["comparison"]["pnl_diff"] == "0"
    assert eth["note"].startswith("periodo")
    assert {c["code"] for c in report["live_gate"]["criteria"]} == {
        "operating_days",
        "max_drawdown",
        "profit_factor",
        "daily_loss",
    }
    markdown = to_markdown(report)
    assert "Paper x backtest" in markdown
    assert "nao comparado" in markdown


def test_reports_are_written_as_json_and_markdown(tmp_path: Path) -> None:
    report = build_cumulative(inputs([trade("a")]))
    json_path, md_path = write_report(tmp_path / "reports" / "paper" / "r1", "cumulative", report)

    assert json.loads(json_path.read_text(encoding="utf-8"))["run_id"] == "r1"
    assert md_path.read_text(encoding="utf-8").startswith("# Paper r1")
