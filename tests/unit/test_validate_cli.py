import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from engine.application.strategies.registry import STRATEGY_REGISTRY
from engine.domain.types import Timeframe
from engine.runner import validate
from engine.runner.selector_store import read_samples, samples_path
from tests.synthetic import write_synthetic_cache

END = datetime(2025, 12, 1, tzinfo=UTC)
DAYS = 292
NAMES = "ema_cross_trend,elephant_bar_breakout"


def argv(tmp_path: Path, out: str, *extra: str) -> list[str]:
    return [
        "--symbols", "BTC/USDT",
        "--timeframes", "4h",
        "--years", "0.8",
        "--end", END.isoformat(),
        "--strategies", NAMES,
        "--simulations", "100",
        "--train-days", "120",
        "--test-days", "60",
        "--no-db",
        "--cache-dir", str(tmp_path / "cache"),
        "--out", str(tmp_path / out),
        "--samples-dir", str(tmp_path / "samples"),
        *extra,
    ]  # fmt: skip


def run(args: list[str]) -> int:
    with pytest.raises(SystemExit) as exit_info:
        validate.main(args)
    return int(exit_info.value.code or 0)


@pytest.fixture
def synthetic(tmp_path: Path) -> Path:
    start = END - timedelta(days=DAYS)
    write_synthetic_cache(tmp_path / "cache", "BTC/USDT", Timeframe.H4, start, DAYS)
    return tmp_path


def summary_of(root: Path) -> dict[str, object]:
    [folder] = [p for p in root.iterdir() if p.is_dir()]
    return json.loads((folder / "summary.json").read_text(encoding="utf-8"))["summary"]  # type: ignore[no-any-return]


def test_cli_runs_on_synthetic_data_and_writes_reports(synthetic: Path) -> None:
    assert run(argv(synthetic, "out")) == 0

    [folder] = list((synthetic / "out").iterdir())
    assert {p.name for p in folder.iterdir()} == {"summary.json", "summary.md", "BTCUSDT_4h"}
    cell = {p.name for p in (folder / "BTCUSDT_4h").iterdir()}
    assert {"validation.json", "validation.md", "samples.jsonl", "idle.json"} <= cell
    summary = summary_of(synthetic / "out")
    assert len(summary["comparisons"]) == 3  # type: ignore[arg-type]
    assert summary["alpha_corrected"] < summary["alpha"]  # type: ignore[operator]
    text = (folder / "summary.md").read_text(encoding="utf-8")
    assert "Lista das comparações testadas" in text and "Conclusão" in text


def test_cli_is_deterministic_and_resume_reuses_samples(synthetic: Path) -> None:
    assert run(argv(synthetic, "a")) == 0
    assert run(argv(synthetic, "b", "--resume")) == 0
    assert summary_of(synthetic / "a") == summary_of(synthetic / "b")


def test_family_correction_counts_all_cells(synthetic: Path) -> None:
    start = END - timedelta(days=DAYS)
    write_synthetic_cache(synthetic / "cache", "ETH/USDT", Timeframe.H4, start, DAYS)
    extra = ["--symbols", "BTC/USDT,ETH/USDT", "--simulations", "300"]
    assert run([*argv(synthetic, "out"), *extra]) == 0
    summary = summary_of(synthetic / "out")
    assert len(summary["comparisons"]) == 6  # type: ignore[arg-type]
    first = summary["reports"][0]["selector"]["judgement"]  # type: ignore[index]
    assert first["comparisons"] == 6


def test_publish_is_refused_without_a_favorable_verdict(synthetic: Path) -> None:
    code = run(argv(synthetic, "out", "--publish-scores"))
    summary = summary_of(synthetic / "out")
    if summary["any_passed"]:
        pytest.skip("o seletor passou nos dados sinteticos: publicar exigiria banco")
    assert code == validate.EXIT_NOT_VALIDATED
    assert not samples_path(synthetic / "samples", "4h").exists()


def test_unknown_strategy_is_a_usage_error(synthetic: Path) -> None:
    args = argv(synthetic, "out")
    args[args.index(NAMES)] = "nao_existe"
    assert run(args) == validate.EXIT_USAGE


def test_registry_strategies_are_selectable() -> None:
    assert [s.name for s in validate.select_strategies("all")] == list(STRATEGY_REGISTRY)
    assert read_samples(Path("nao-existe.jsonl")) == []


def test_underpowered_simulations_are_refused_unless_allowed(synthetic: Path) -> None:
    weak = ["--simulations", "50", "--alpha", "0.01"]
    assert run([*argv(synthetic, "out"), *weak]) == validate.EXIT_USAGE
    assert not (synthetic / "out").exists() or not list((synthetic / "out").iterdir())

    assert run([*argv(synthetic, "ok"), *weak, "--allow-underpowered"]) == 0
    summary = summary_of(synthetic / "ok")
    assert summary["underpowered"] is True and not summary["any_passed"]
    assert "sem poder" in (next((synthetic / "ok").iterdir()) / "summary.md").read_text(encoding="utf-8")


def test_default_simulations_reach_the_corrected_alpha() -> None:
    args = validate.build_parser().parse_args(["--end", END.isoformat()])
    m = validate.count_comparisons(list(STRATEGY_REGISTRY), ["A", "B", "C"], ["1h", "4h"])
    assert m == (len(STRATEGY_REGISTRY) * 2 - 1 + 2) * 3
    assert validate.resolve_simulations(args, m) >= 10_000


def test_vwap_is_not_counted_on_4h() -> None:
    assert validate.count_comparisons(["vwap_reclaim", "macd_cross"], ["A"], ["1h", "4h"]) == 5
