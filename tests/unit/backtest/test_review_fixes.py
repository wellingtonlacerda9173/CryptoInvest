"""Regressoes da segunda rodada de correcoes do backtest (dimensionamento, vetos, dia UTC, veredito, CLI)."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from engine.application.backtest import (
    BacktestResult,
    DecisionAction,
    DecisionRecord,
    EquityPoint,
    HypotheticalOutcome,
    HypotheticalStatus,
    NotEnoughCandlesError,
    RoundTrip,
    compute_active_metrics,
    default_rules,
    metrics_from_parts,
    simulate_signal,
    summarize_avoided,
    walk_forward,
)
from engine.application.backtest.walk_forward import WindowResult, _assess
from engine.application.risk import AuditAction, AuditEntry
from engine.domain.signal import Signal
from engine.domain.types import Timeframe
from engine.runner import backtest as cli
from engine.runner.candle_source import LoadedCandles
from tests.unit.backtest.engine_helpers import ENTRY_CANDLE, QUIET, T0, flat_then, mk, run, settings
from tests.unit.backtest.test_metrics import curve, trip
from tests.unit.backtest.test_walk_forward import CONFIG, GRID, factory

D = Decimal
MIDNIGHT = datetime(2026, 2, 1, tzinfo=UTC)
SIGNAL = Signal(
    symbol="BTC/USDT",
    timeframe=Timeframe.H1,
    created_at=T0,
    strategy="t",
    entry=D(100),
    stop_loss=D(95),
    take_profit=D(111),
)


def test_dust_sized_signal_is_not_simulated() -> None:
    candles = flat_then([ENTRY_CANDLE, QUIET])

    outcome = simulate_signal(
        SIGNAL, candles, 29, equity=D(1), settings=settings(), rules=default_rules("BTC/USDT")
    )

    assert outcome.status is HypotheticalStatus.NOT_SIMULATED
    assert outcome.pnl == 0


def veto(code: str, pnl: int) -> DecisionRecord:
    return DecisionRecord(
        at=T0,
        strategy="t",
        symbol="BTC/USDT",
        action=DecisionAction.SKIPPED,
        code=code,
        message="m",
        hypothetical=HypotheticalOutcome(
            status=HypotheticalStatus.TARGET if pnl > 0 else HypotheticalStatus.STOP,
            pnl=D(pnl),
            r_multiple=D(0),
            candles=1,
        ),
    )


def test_state_locks_and_capacity_stay_out_of_the_risk_filter_effect_and_halt_has_its_date() -> None:
    halted_at = T0 + timedelta(days=3)
    decisions = [
        veto("low_liquidity", -30),
        veto("spread_too_wide", 10),
        veto("robot_halted", 100),
        veto("robot_halted", -20),
        veto("daily_loss_pause", 5),
        veto("max_positions", 7),
    ]
    events = [AuditEntry(at=halted_at, action=AuditAction.HALTED, detail="drawdown")]

    summary = summarize_avoided(decisions, events)

    assert summary.risk_vetoes_net_effect == D(20)
    assert summary.state_locks_net_effect == D(-80) - D(5)
    assert summary.capacity_net_effect == D(-7)
    assert summary.halt_at == halted_at
    assert (summary.halt_signals, summary.halt_net_effect) == (2, D(-80))
    assert {g.code: g.kind for g in summary.by_code} == {
        "daily_loss_pause": "state_lock",
        "low_liquidity": "filter",
        "max_positions": "capacity",
        "robot_halted": "state_lock",
        "spread_too_wide": "filter",
    }


async def test_active_metrics_cover_only_the_stretch_before_the_halt() -> None:
    base = await run(flat_then([QUIET]))
    halted_at = datetime(2026, 1, 30, 13, tzinfo=UTC)
    result = BacktestResult.model_validate(
        {
            **base.model_dump(),
            "initial_capital": D(1000),
            "equity_curve": curve([1100, 900, 900]),
            "round_trips": (trip(100, day=0), trip(-200, day=1)),
            "risk_events": (AuditEntry(at=halted_at, action=AuditAction.HALTED, detail="x"),),
        }
    )

    active = compute_active_metrics(result)

    assert active is not None
    assert (active.trades, active.final_equity) == (1, D(1100))
    assert compute_active_metrics(base) is None


def point(at: datetime, equity: int) -> EquityPoint:
    return EquityPoint(at=at, equity=D(equity), realized=D(equity), in_position=False)


class TestDayAndMonthAttribution:
    def test_midnight_equity_point_belongs_to_the_candle_that_closed(self) -> None:
        points = [
            EquityPoint(
                at=MIDNIGHT - timedelta(hours=1), equity=D(1050), realized=D(1050), in_position=False
            ),
            point(MIDNIGHT, 1100),
            EquityPoint(
                at=MIDNIGHT + timedelta(hours=1), equity=D(1000), realized=D(1000), in_position=False
            ),
        ]

        january, february = metrics_from_parts(D(1000), points, []).monthly

        assert january.return_pct == pytest.approx(0.10)
        assert february.return_pct == pytest.approx(1000 / 1100 - 1)

    def test_intra_candle_exit_at_midnight_counts_in_the_month_that_ended(self) -> None:
        intra: RoundTrip = trip(-10).model_copy(update={"closed_at": MIDNIGHT, "closed_intra_candle": True})
        at_open: RoundTrip = trip(-10).model_copy(update={"closed_at": MIDNIGHT})
        points = curve([1000, 1000, 1000])

        monthly = metrics_from_parts(D(1000), points, [intra, at_open]).monthly

        assert [(m.month, m.trades) for m in monthly] == [("2026-01", 1), ("2026-02", 1)]


class TestExitLabels:
    async def test_profitable_moved_stop_is_labelled_trailing_stop(self) -> None:
        win = [
            ENTRY_CANDLE,
            ("100.5", "112", "100", "111.5"),
            ("111.5", "120", "111", "119"),
            ("119", "119.5", "114", "115"),
            QUIET,
        ]

        result = await run(flat_then(win))

        [closed] = result.round_trips
        assert closed.exit_reasons[-1].value == "stop_loss"
        assert closed.labels == ("take_profit", "trailing_stop")

    async def test_plain_stop_loss_keeps_its_label(self) -> None:
        result = await run(flat_then([ENTRY_CANDLE, ("100.5", "101", "94", "94.5"), QUIET]))

        assert result.round_trips[0].labels == ("stop_loss",)


def window(train_trip: RoundTrip) -> WindowResult:
    return WindowResult(
        index=1,
        train_start=T0,
        train_end=T0,
        test_start=T0,
        test_end=T0,
        candidates=(),
        selected={"rr": D(3)},
        train_metrics=metrics_from_parts(D(1000), curve([1100]), [train_trip]),
        test_metrics=None,
        test_result=None,
    )


class TestVerdictCategories:
    def test_no_edge_anywhere_is_not_reported_as_overfit(self) -> None:
        losing = window(trip(-10))
        oos = metrics_from_parts(D(1000), curve([900]), [trip(-10, day=i) for i in range(30)])

        assert _assess([losing, losing], oos, CONFIG).verdict == "sem edge detectavel fora da amostra"

    def test_edge_in_sample_that_vanishes_out_of_sample_is_overfit(self) -> None:
        good = window(trip(100))
        oos = metrics_from_parts(D(1000), curve([900]), [trip(-10, day=i) for i in range(30)])

        assert _assess([good, good], oos, CONFIG).verdict == "indicios de sobreajuste"

    def test_stable_positive_edge_is_consistent(self) -> None:
        good = window(trip(100))
        oos = metrics_from_parts(D(1000), curve([1100]), [trip(10, day=i) for i in range(30)])

        assert _assess([good, good], oos, CONFIG).verdict == "consistente"

    def test_small_sample_is_inconclusive_even_with_a_bad_result(self) -> None:
        good = window(trip(100))
        oos = metrics_from_parts(D(1000), curve([900]), [trip(-10, day=i) for i in range(29)])

        assert _assess([good], oos, CONFIG).verdict == "inconclusivo (amostra pequena)"


class TestCli:
    @pytest.mark.parametrize(
        "text", ["2026-10-18T00:00:00", "2026-10-18T00:00:00+00:00", "2026-10-17T21:00:00-03:00"]
    )
    def test_instants_without_timezone_are_utc_and_offsets_are_normalised(self, text: str) -> None:
        assert cli.parse_instant(text) == datetime(2026, 10, 18, tzinfo=UTC)
        assert cli.parse_instant(text).utcoffset() == timedelta(0)

    def test_resolved_period_is_utc_and_independent_of_the_local_timezone(self) -> None:
        args = cli.build_parser().parse_args(["--end", "2026-10-18T00:00:00", "--years", "1"])

        start, end = cli.resolve_period(args, datetime.now(UTC), Timeframe.H1)

        assert end == datetime(2026, 10, 18, tzinfo=UTC)
        assert start == end - timedelta(days=365)

    async def test_walk_forward_needs_a_full_window(self) -> None:
        candles = [mk(i, "100", "101", "99", "100") for i in range(48)]

        with pytest.raises(NotEnoughCandlesError, match="walk-forward"):
            await walk_forward(candles, factory, GRID, settings(), CONFIG)

    def test_short_period_prints_a_friendly_message_and_reports_have_no_absolute_paths(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        candles = [mk(i, "100", "100.2", "99.8", "100") for i in range(96)]

        async def fake_load(*_: object, **__: object) -> LoadedCandles:
            return LoadedCandles(candles, "teste", ())

        monkeypatch.setattr(cli, "load_candles", fake_load)
        argv = ["--end", "2026-01-05T00:00:00", "--years", "0.01", "--walk-forward", "--no-db"]
        argv += ["--out", str(tmp_path / "out"), "--cache-dir", str(tmp_path / "cache")]

        with pytest.raises(SystemExit) as raised:
            cli.main(argv)

        assert raised.value.code == 1
        assert "walk-forward nao executado" in capsys.readouterr().err
        report = (tmp_path / "out" / "BTCUSDT_1h_elephant-ema" / "report.json").read_text(encoding="utf-8")
        assert str(tmp_path) not in report
        assert json.loads(report)["meta"]["parameters"]["period"]["end"] == "2026-01-05T00:00:00+00:00"
