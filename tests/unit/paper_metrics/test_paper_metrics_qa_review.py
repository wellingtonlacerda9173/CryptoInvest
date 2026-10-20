"""Evidencias da revisao QA da trava do Live (os bugs confirmados na Fase 3 foram corrigidos)."""

from datetime import timedelta
from decimal import Decimal

from engine.application.paper_metrics import (
    LiveGateThresholds,
    compute_paper_metrics,
    evaluate_live_gate,
    operating_days,
)
from engine.domain.journal import ControlAction
from tests.unit.paper_metrics.test_paper_metrics import T0, alive, event, trade

D = Decimal


def test_gate_does_not_approve_when_drawdown_cannot_be_measured() -> None:
    now = T0 + timedelta(days=70)
    metrics = compute_paper_metrics(
        initial_equity=D(10_000),
        trades=[trade("500", day=d) for d in range(0, 3)] + [trade("-10", day=3)],
        curve=[],
        events=[event(ControlAction.START, T0)],
        now=now,
        heartbeats=alive(70),
    )
    report = evaluate_live_gate(metrics, LiveGateThresholds(max_daily_loss=D("0.01")))
    criterion = next(c for c in report.criteria if c.code == "max_drawdown")
    assert not criterion.met
    assert criterion.actual == "nao medido"
    assert not report.approved


def test_dead_process_without_stop_event_does_not_count_operating_days() -> None:
    # so ha evento START e nenhuma evidencia de vida (snapshot/heartbeat): nao ha dia comprovado
    now = T0 + timedelta(days=61)
    metrics = compute_paper_metrics(
        initial_equity=D(10_000),
        trades=[trade("100", day=1)],
        curve=[],
        events=[event(ControlAction.START, T0)],
        now=now,
    )
    assert metrics.operating_days == 0


def test_process_that_died_after_ten_days_counts_only_those_days() -> None:
    metrics = compute_paper_metrics(
        initial_equity=D(10_000),
        trades=[],
        curve=[],
        events=[event(ControlAction.START, T0)],
        now=T0 + timedelta(days=61),
        heartbeats=alive(10),
    )
    assert metrics.operating_days == 10


def test_single_winning_trade_does_not_satisfy_profit_factor() -> None:
    now = T0 + timedelta(days=61)
    metrics = compute_paper_metrics(
        initial_equity=D(10_000),
        trades=[trade("1", day=1)],
        curve=[],
        events=[event(ControlAction.START, T0)],
        now=now,
    )
    report = evaluate_live_gate(metrics, LiveGateThresholds(max_daily_loss=D("0.01")))
    criterion = next(c for c in report.criteria if c.code == "profit_factor")
    assert not criterion.met
    assert criterion.reason is not None
    assert "amostra insuficiente" in criterion.reason


def test_operating_day_counts_at_exactly_ninety_percent_and_not_below() -> None:
    now = T0 + timedelta(days=1)
    exact = [event(ControlAction.START, T0 + timedelta(hours=2, minutes=24))]
    below = [event(ControlAction.START, T0 + timedelta(hours=2, minutes=24, seconds=1))]

    assert operating_days(exact, now) == 1
    assert operating_days(below, now) == 0


def test_repeated_start_does_not_reset_the_active_interval() -> None:
    events = [event(ControlAction.START, T0), event(ControlAction.START, T0 + timedelta(hours=12))]

    assert operating_days(events, T0 + timedelta(days=1)) == 1
