from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from engine.application.backtest.models import EquityPoint
from engine.application.paper_metrics import (
    DEFAULT_MATCH_TOLERANCE,
    LiveGateThresholds,
    PeriodPerformance,
    active_intervals,
    alive_intervals,
    compare_paper_backtest,
    compute_paper_metrics,
    curve_covers,
    drawdown,
    evaluate_live_gate,
    ledger_drawdown,
    operating_days,
    payoff,
    profit_factor,
    worst_daily_loss,
)
from engine.application.paper_metrics.metrics import DEFAULT_MAX_CURVE_GAP
from engine.domain.journal import ControlAction, ControlEvent
from engine.domain.risk import RiskConfig
from engine.domain.trade import ExitReason, Trade

D = Decimal
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def event(action: ControlAction, at: datetime) -> ControlEvent:
    return ControlEvent(at=at, action=action, operator="ana", reason="teste")


def trade(
    pnl: str, day: int, hour: int = 12, *, symbol: str = "BTC/USDT", trade_id: str | None = None
) -> Trade:
    """Trade de 1 unidade comprada a 100 e vendida a 100 + pnl, sem taxas."""
    closed = T0 + timedelta(days=day, hours=hour)
    return Trade(
        trade_id=trade_id,
        symbol=symbol,
        quantity=D(1),
        entry_price=D(100),
        exit_price=D(100) + D(pnl),
        opened_at=closed - timedelta(hours=2),
        closed_at=closed,
        exit_reason=ExitReason.TAKE_PROFIT if D(pnl) > 0 else ExitReason.STOP_LOSS,
    )


def point(day: int, equity: str) -> EquityPoint:
    return EquityPoint(
        at=T0 + timedelta(days=day, hours=23), equity=D(equity), realized=D(equity), in_position=False
    )


def point_at(offset: timedelta, equity: str = "1000") -> EquityPoint:
    return EquityPoint(at=T0 + offset, equity=D(equity), realized=D(equity), in_position=False)


def daily_curve(days: int, overrides: dict[int, str] | None = None) -> list[EquityPoint]:
    """Um ponto por dia (23h) com capital 1000, salvo nos dias de `overrides`."""
    return [point(d, (overrides or {}).get(d, "1000")) for d in range(days)]


def alive(days: int) -> list[datetime]:
    """Heartbeat de hora em hora desde T0."""
    return [T0 + timedelta(hours=h) for h in range(days * 24)]


def test_operating_days_counts_only_completed_active_utc_days() -> None:
    events = [event(ControlAction.START, T0)]

    assert operating_days(events, T0 + timedelta(days=10, hours=12)) == 10
    assert operating_days(events, T0 + timedelta(hours=23)) == 0


def test_operating_days_discard_days_with_long_pause_but_tolerate_short_restart() -> None:
    events = [
        event(ControlAction.START, T0),
        event(ControlAction.PAUSE, T0 + timedelta(days=2, hours=6)),
        event(ControlAction.RESUME, T0 + timedelta(days=2, hours=8)),
        event(ControlAction.PAUSE, T0 + timedelta(days=4)),
        event(ControlAction.RESUME, T0 + timedelta(days=5, hours=12)),
    ]

    # dias 0,1 inteiros; dia 2 com 22h/24h (>= 90%); dia 3 inteiro; dia 4 parado; dia 5 so 12h (50%); dias 6-7
    assert operating_days(events, T0 + timedelta(days=8)) == 6


def test_halt_stops_the_clock_until_reactivation() -> None:
    events = [
        event(ControlAction.START, T0),
        event(ControlAction.HALT, T0 + timedelta(days=3)),
        event(ControlAction.REACTIVATE, T0 + timedelta(days=6)),
    ]

    assert active_intervals(events, T0 + timedelta(days=8)) == [
        (T0, T0 + timedelta(days=3)),
        (T0 + timedelta(days=6), T0 + timedelta(days=8)),
    ]
    assert operating_days(events, T0 + timedelta(days=8)) == 5


def test_no_start_event_means_no_operating_days() -> None:
    assert operating_days([], T0 + timedelta(days=90)) == 0


def test_operating_days_require_aware_now() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        operating_days([event(ControlAction.START, T0)], datetime(2026, 3, 1))


def test_operating_days_need_liveness_evidence_not_only_a_start_event() -> None:
    events = [event(ControlAction.START, T0)]
    now = T0 + timedelta(days=30)

    assert operating_days(events, now, liveness=[]) == 0
    assert operating_days(events, now, liveness=alive(10)) == 10
    assert operating_days(events, now) == 30


def test_operating_days_tolerate_a_missed_heartbeat_but_not_a_long_outage() -> None:
    events = [event(ControlAction.START, T0)]
    now = T0 + timedelta(days=3)

    one_missed = [t for t in alive(3) if t != T0 + timedelta(hours=5)]
    assert operating_days(events, now, liveness=one_missed) == 3

    out_from, out_until = T0 + timedelta(days=1, hours=2), T0 + timedelta(days=1, hours=9)
    outage = [t for t in alive(3) if not out_from <= t < out_until]
    assert operating_days(events, now, liveness=outage) == 2


def test_operating_days_combine_control_events_and_liveness() -> None:
    events = [event(ControlAction.START, T0), event(ControlAction.PAUSE, T0 + timedelta(days=2))]

    assert operating_days(events, T0 + timedelta(days=5), liveness=alive(5)) == 2


def test_snapshots_of_the_equity_curve_count_as_liveness() -> None:
    hourly = [EquityPoint(at=at, equity=D(1000), realized=D(1000), in_position=False) for at in alive(10)]

    metrics = compute_paper_metrics(
        initial_equity=D(1000),
        trades=[],
        curve=hourly,
        events=[event(ControlAction.START, T0)],
        now=T0 + timedelta(days=10),
    )

    assert metrics.operating_days == 10


def test_alive_intervals_merge_and_clip_at_now() -> None:
    now = T0 + timedelta(hours=10)
    beats = [T0, T0 + timedelta(hours=1), T0 + timedelta(hours=6), T0 + timedelta(hours=9)]

    assert alive_intervals(beats, timedelta(hours=2), now) == [
        (T0, T0 + timedelta(hours=3)),
        (T0 + timedelta(hours=6), T0 + timedelta(hours=8)),
        (T0 + timedelta(hours=9), now),
    ]
    assert alive_intervals([now + timedelta(hours=1)], timedelta(hours=2), now) == []


def test_profit_factor_and_payoff_by_hand() -> None:
    pnls = [D(30), D(-10), D(20), D(-20), D(10)]

    # ganhos 60 / perdas 30; media ganho 20 / media perda 15
    assert profit_factor(pnls) == D(2)
    assert payoff(pnls) == D(20) / D(15)


def test_profit_factor_and_payoff_undefined_without_losses_or_gains() -> None:
    assert profit_factor([D(10), D(5)]) is None
    assert payoff([D(10)]) is None
    assert payoff([D(-10)]) is None
    assert profit_factor([]) is None


def test_worst_daily_loss_is_relative_to_equity_at_start_of_that_day() -> None:
    trades = [
        trade("-20", 0, 9),
        trade("10", 0, 15),  # dia 0: -10 sobre 1000 (1%)
        trade("-50", 1),  # dia 1: -50 sobre 990 (5,05%)
        trade("100", 2),
        trade("-5", 3),
    ]

    amount, pct = worst_daily_loss(D(1000), trades)

    assert amount == D(50)
    assert pct == D(50) / D(990)


def test_worst_daily_loss_zero_when_no_losing_day() -> None:
    assert worst_daily_loss(D(1000), [trade("10", 0), trade("-5", 0)]) == (D(0), D(0))


def test_compute_paper_metrics_aggregates_everything() -> None:
    trades = [trade("30", 0), trade("-10", 1), trade("20", 2)]
    curve = [point(0, "1030"), point(1, "1020"), point(2, "1040")]
    metrics = compute_paper_metrics(
        initial_equity=D(1000),
        trades=trades,
        curve=curve,
        events=[event(ControlAction.START, T0)],
        now=T0 + timedelta(days=3),
        heartbeats=alive(3),
        max_curve_gap=timedelta(days=1),
    )

    assert metrics.operating_days == 3
    assert metrics.trades == 3
    assert metrics.losing_trades == 1
    assert metrics.total_pnl == D(40)
    assert (metrics.gross_profit, metrics.gross_loss) == (D(50), D(10))
    assert metrics.profit_factor == D(5)
    assert metrics.payoff == D(25) / D(10)
    # pico 1030, vale 1020: 10/1030
    assert metrics.max_drawdown == (D(10) / D(1030)).quantize(D("1e-12"))
    assert metrics.worst_daily_loss == D(10)
    assert metrics.worst_daily_loss_pct == D(10) / D(1030)


def test_drawdown_is_decimal_quantized_and_starts_from_initial_equity() -> None:
    assert drawdown(D(1000), [point(0, "900"), point(1, "1100"), point(2, "990")]) == D("0.1")
    assert drawdown(D(1000), []) == D(0)
    third = drawdown(D(3), [point(0, "2")])
    assert isinstance(third, Decimal)
    assert third == (D(1) / D(3)).quantize(D("1e-12"))


def test_curve_covers_requires_points_and_no_large_gaps_including_edges() -> None:
    start, end = T0, T0 + timedelta(days=3)

    day = timedelta(days=1)

    assert not curve_covers([], start, end, day)
    assert curve_covers(daily_curve(3), start, end, day)
    assert not curve_covers(daily_curve(2), start, end, day)  # ponta final descoberta
    assert not curve_covers([point(1, "1000"), point(2, "1000")], start, end, day)  # inicio descoberto
    assert not curve_covers([point(0, "1000"), point(2, "1000")], start, end, day)  # buraco no meio


def test_curve_covers_defaults_to_the_liveness_window_so_daily_snapshots_do_not_measure() -> None:
    assert timedelta(hours=2) == DEFAULT_MAX_CURVE_GAP
    assert not curve_covers(daily_curve(3), T0, T0 + timedelta(days=3))


def test_curve_covers_tolerates_a_hole_only_while_the_robot_was_stopped() -> None:
    gap = timedelta(hours=2)
    curve = [
        point_at(timedelta(hours=1)),
        point_at(timedelta(hours=2)),
        point_at(timedelta(days=3, hours=1)),
        point_at(timedelta(days=3, hours=2)),
    ]
    end = T0 + timedelta(days=3, hours=3)
    stopped = [(T0, T0 + timedelta(hours=2)), (T0 + timedelta(days=3), end)]

    assert not curve_covers(curve, T0, end, gap)
    assert not curve_covers(curve, T0, end, gap, active=[(T0, end)])  # sem stop registrado
    assert curve_covers(curve, T0, end, gap, active=stopped)
    # stop tardio: o trecho ativo sem snapshot passa da janela
    late_stop = [(T0, T0 + timedelta(hours=5)), (T0 + timedelta(days=3), end)]
    assert not curve_covers(curve, T0, end, gap, active=late_stop)


def approved_inputs() -> dict[str, object]:
    # 60 dias: 30 ganhos de +2 e 30 perdas de -1 (PF 2); a maior perda do dia e 0,1% do capital
    trades = [trade("2", d) for d in range(0, 60, 2)] + [trade("-1", d, 20) for d in range(1, 60, 2)]
    return {
        "initial_equity": D(1000),
        "trades": trades,
        "curve": daily_curve(60),
        "events": [event(ControlAction.START, T0)],
        "now": T0 + timedelta(days=60),
        "heartbeats": alive(60),
        "max_curve_gap": timedelta(days=1),  # estes testes usam snapshots diarios
    }


def long_run(days: int) -> dict[str, object]:
    return {"now": T0 + timedelta(days=days), "curve": daily_curve(days), "heartbeats": alive(days)}


def gate(**overrides: object):  # type: ignore[no-untyped-def]
    inputs = approved_inputs() | overrides
    thresholds = LiveGateThresholds.from_risk(RiskConfig())
    return evaluate_live_gate(compute_paper_metrics(**inputs), thresholds)  # type: ignore[arg-type]


def test_live_gate_approves_when_every_criterion_is_met() -> None:
    report = gate()

    assert report.approved
    assert [c.code for c in report.criteria] == [
        "operating_days",
        "max_drawdown",
        "profit_factor",
        "daily_loss",
    ]
    assert report.failed == ()
    assert report.metrics.operating_days == 60
    assert report.metrics.profit_factor == D(2)


def test_live_gate_blocks_before_60_days() -> None:
    report = gate(now=T0 + timedelta(days=59, hours=12))

    assert not report.approved
    assert [c.code for c in report.failed] == ["operating_days"]
    assert report.failed[0].actual == "59"


def test_live_gate_blocks_drawdown_at_or_above_5_percent() -> None:
    report = gate(curve=daily_curve(60, {1: "950"}))

    assert [c.code for c in report.failed] == ["max_drawdown"]
    assert report.failed[0].actual == "5.00%"


def test_live_gate_accepts_drawdown_just_below_limit() -> None:
    assert gate(curve=daily_curve(60, {1: "951"})).approved


def test_live_gate_blocks_profit_factor_not_above_1_5() -> None:
    # 15 ganhos de 3 e 15 perdas de 2 (amostra minima atendida): PF exatamente 1,5
    trades = [trade("3", d) for d in range(15)] + [trade("-2", d, 20) for d in range(15)]

    report = gate(trades=trades)

    assert [c.code for c in report.failed] == ["profit_factor"]
    assert report.failed[0].reason is None


def test_live_gate_blocks_any_day_with_loss_above_daily_limit() -> None:
    # dia 60: +30 leva o capital a 1060 (limite de 1% = 10,6); o dia 61 perde 10,7
    trades = [*approved_inputs()["trades"], trade("30", 60), trade("-10.7", 61)]  # type: ignore[misc]

    report = gate(trades=trades, **long_run(62))

    assert [c.code for c in report.failed] == ["daily_loss"]


def test_live_gate_loss_exactly_at_limit_is_allowed() -> None:
    trades = [*approved_inputs()["trades"], trade("30", 60), trade("-10.6", 61)]  # type: ignore[misc]

    assert gate(trades=trades, **long_run(62)).approved


def test_live_gate_no_trades_is_not_approved() -> None:
    report = gate(trades=[])

    assert [c.code for c in report.failed] == ["profit_factor"]


def test_live_gate_no_losses_is_not_enough_to_trust_the_profit_factor() -> None:
    report = gate(trades=[trade("5", d) for d in range(40)])

    assert [c.code for c in report.failed] == ["profit_factor"]
    assert report.failed[0].reason == "amostra insuficiente: 0 perdas (minimo 5)"


def test_live_gate_requires_minimum_trades_and_losses_and_says_why() -> None:
    few = [trade("3", d) for d in range(4)] + [trade("-1", d, 20) for d in range(5)]

    report = gate(trades=few)

    assert [c.code for c in report.failed] == ["profit_factor"]
    assert report.failed[0].reason == "amostra insuficiente: 9 trades (minimo 30)"


def test_live_gate_sample_thresholds_are_configurable() -> None:
    trades = [trade("3", d) for d in range(4)] + [trade("-1", d, 20) for d in range(2)]
    metrics = compute_paper_metrics(**(approved_inputs() | {"trades": trades}))  # type: ignore[arg-type]

    strict = evaluate_live_gate(metrics, LiveGateThresholds(max_daily_loss=D("0.01")))
    relaxed = evaluate_live_gate(
        metrics, LiveGateThresholds(max_daily_loss=D("0.01"), min_trades=6, min_losses=2)
    )

    assert not strict.approved
    assert relaxed.approved


def test_live_gate_unmeasured_drawdown_fails_with_reason() -> None:
    report = gate(curve=[])

    assert [c.code for c in report.failed] == ["max_drawdown"]
    assert report.failed[0].actual == "nao medido"
    assert report.failed[0].reason is not None
    assert report.metrics.max_drawdown is None


def test_live_gate_fails_closed_without_liveness_evidence() -> None:
    report = gate(curve=[], heartbeats=[])

    assert {c.code for c in report.failed} == {"operating_days", "max_drawdown"}
    assert report.metrics.operating_days == 0


def test_compare_paper_backtest_by_hand() -> None:
    start, end = T0, T0 + timedelta(days=10)
    paper = PeriodPerformance(
        initial_equity=D(1000),
        trades=(trade("30", 1), trade("-10", 3, symbol="ETH/USDT"), trade("5", 20)),  # a de fora do periodo
    )
    backtest = PeriodPerformance(
        initial_equity=D(2000),
        trades=(trade("40", 1), trade("-20", 5), trade("10", 6, symbol="ETH/USDT")),
    )

    result = compare_paper_backtest(paper, backtest, start=start, end=end)

    assert (result.paper_trades, result.backtest_trades, result.trades_diff) == (2, 3, -1)
    assert (result.paper_pnl, result.backtest_pnl, result.pnl_diff) == (D(20), D(30), D(-10))
    assert result.paper_return == D("0.02")
    assert result.backtest_return == D("0.015")
    assert result.return_diff == D("0.005")
    # so o BTC do dia 1 casa (mesmo simbolo e abertura); o ETH abriu em horarios diferentes
    assert (result.matched, result.only_paper, result.only_backtest) == (1, 1, 2)


def test_compare_matches_within_tolerance_and_uses_each_backtest_trade_once() -> None:
    paper = PeriodPerformance(initial_equity=D(1000), trades=(trade("5", 1, 12), trade("5", 1, 12)))
    backtest = PeriodPerformance(initial_equity=D(1000), trades=(trade("5", 1, 13),))

    exact = compare_paper_backtest(paper, backtest, start=T0, end=T0 + timedelta(days=2))
    loose = compare_paper_backtest(
        paper, backtest, start=T0, end=T0 + timedelta(days=2), match_tolerance=timedelta(hours=1)
    )

    assert exact.matched == 0
    assert (loose.matched, loose.only_paper, loose.only_backtest) == (1, 1, 0)


def test_compare_default_tolerance_matches_small_opening_drift_only() -> None:
    assert timedelta(minutes=10) == DEFAULT_MATCH_TOLERANCE
    base = trade("5", 1, 12)
    paper = PeriodPerformance(initial_equity=D(1000), trades=(base,))

    def matched_with_shift(shift: timedelta) -> int:
        other = base.model_copy(
            update={"opened_at": base.opened_at + shift, "closed_at": base.closed_at + shift}
        )
        backtest = PeriodPerformance(initial_equity=D(1000), trades=(other,))
        return compare_paper_backtest(paper, backtest, start=T0, end=T0 + timedelta(days=2)).matched

    assert matched_with_shift(timedelta(minutes=10)) == 1
    assert matched_with_shift(timedelta(minutes=10, seconds=1)) == 0


def test_compare_rejects_empty_period() -> None:
    empty = PeriodPerformance(initial_equity=D(1000), trades=())

    with pytest.raises(ValueError, match="after start"):
        compare_paper_backtest(empty, empty, start=T0, end=T0)


def test_ledger_drawdown_catches_the_intraday_low_between_snapshots() -> None:
    round_trip = [trade("-60", day=1, hour=10), trade("60", day=1, hour=14)]

    assert ledger_drawdown(D(1000), round_trip) == D("0.06")
    assert ledger_drawdown(D(1000), []) == D(0)
    assert ledger_drawdown(D(1000), [trade("50", day=1), trade("-25", day=2)]) == (D(25) / D(1050)).quantize(
        D("1e-12")
    )
    # a ordem e a de fechamento, nao a da lista
    assert ledger_drawdown(D(1000), list(reversed(round_trip))) == D("0.06")


def test_worst_daily_loss_uses_the_intraday_trough_not_the_net_of_the_day() -> None:
    day = [trade("-60", day=1, hour=10), trade("60", day=1, hour=14)]

    assert worst_daily_loss(D(1000), day) == (D(60), D("0.06"))
    # o dia seguinte parte do capital realizado ate la
    later = [*day, trade("30", day=1, hour=16), trade("-20", day=2, hour=9), trade("5", day=2, hour=11)]
    assert worst_daily_loss(D(1000), later) == (D(60), D("0.06"))
    assert worst_daily_loss(D(1000), [trade("10", day=1)]) == (D(0), D(0))


def test_compute_metrics_takes_the_worse_of_curve_and_ledger_drawdown() -> None:
    now = T0 + timedelta(days=2)
    common = {
        "initial_equity": D(1000),
        "events": [event(ControlAction.START, T0)],
        "now": now,
        "heartbeats": alive(2),
        "max_curve_gap": timedelta(days=1),
    }
    curve = daily_curve(2, {0: "980"})  # a curva mostra -2%; o ledger, -6%
    trades = [trade("-60", day=1, hour=1), trade("60", day=1, hour=2)]

    assert compute_paper_metrics(trades=trades, curve=curve, **common).max_drawdown == D("0.06")  # type: ignore[arg-type]
    assert compute_paper_metrics(trades=[], curve=curve, **common).max_drawdown == D("0.02")  # type: ignore[arg-type]
    assert compute_paper_metrics(trades=trades, curve=[], **common).max_drawdown is None  # type: ignore[arg-type]
