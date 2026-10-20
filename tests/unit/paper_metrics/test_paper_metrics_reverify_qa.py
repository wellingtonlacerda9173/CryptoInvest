"""Reverificacao QA da trava do Live (Fase 3, 2a rodada). `BUG:` + xfail(strict) = defeito confirmado."""

from datetime import timedelta
from decimal import Decimal

from engine.application.backtest.models import EquityPoint
from engine.application.paper_metrics import (
    LiveGateThresholds,
    compute_paper_metrics,
    evaluate_live_gate,
)
from engine.domain.journal import ControlAction
from engine.domain.trade import ExitReason, Trade
from tests.unit.paper_metrics.test_paper_metrics import T0, alive, event, trade

D = Decimal
THRESHOLDS = LiveGateThresholds(max_daily_loss=D("0.02"))


def daily_curve(days: int) -> list[EquityPoint]:
    return [
        EquityPoint(
            at=T0 + timedelta(days=d, hours=23), equity=D(10_000), realized=D(10_000), in_position=False
        )
        for d in range(days)
    ]


def big_trade(pnl: str, day: int, hour: int, trade_id: str) -> Trade:
    """Trade de 1 unidade comprada a 1000: aceita perdas de ate 999 sem preco de saida invalido."""
    closed = T0 + timedelta(days=day, hours=hour)
    return Trade(
        trade_id=trade_id,
        symbol="BTC/USDT",
        quantity=D(1),
        entry_price=D(1000),
        exit_price=D(1000) + D(pnl),
        opened_at=closed - timedelta(hours=2),
        closed_at=closed,
        exit_reason=ExitReason.TAKE_PROFIT if D(pnl) > 0 else ExitReason.STOP_LOSS,
    )


def hourly_curve(daily: list[EquityPoint]) -> list[EquityPoint]:
    """Snapshots de hora em hora (capital 10_000) nos mesmos dias da curva diaria."""
    days = {(p.at - T0).days for p in daily}
    return [
        EquityPoint(at=T0 + timedelta(hours=h), equity=D(10_000), realized=D(10_000), in_position=False)
        for h in range(62 * 24)
        if h // 24 in days
    ]


def _approved_inputs(days: int = 62) -> dict[str, object]:
    wins = [trade("40", day=d, hour=10, trade_id=f"w{d}") for d in range(0, 40)]
    losses = [trade("-10", day=d, hour=14, trade_id=f"l{d}") for d in range(0, 40)]
    return {
        "initial_equity": D(10_000),
        "trades": wins + losses,
        "curve": daily_curve(days),
        "events": [event(ControlAction.START, T0)],
        "now": T0 + timedelta(days=days),
        "heartbeats": alive(days),
        "max_curve_gap": timedelta(days=1),  # snapshots diarios: o ledger realizado cobre o que eles nao veem
    }


def test_a_clean_sixty_day_run_is_approved_and_each_failure_flips_exactly_one_criterion() -> None:
    inputs = _approved_inputs()
    base = evaluate_live_gate(compute_paper_metrics(**inputs), THRESHOLDS)  # type: ignore[arg-type]
    assert base.approved, [c.code for c in base.failed]

    short = compute_paper_metrics(**{**inputs, "heartbeats": alive(59), "now": T0 + timedelta(days=59)})  # type: ignore[arg-type]
    assert [c.code for c in evaluate_live_gate(short, THRESHOLDS).failed] == ["operating_days"]


def test_a_six_percent_drawdown_between_two_daily_snapshots_is_not_invisible_to_the_gate() -> None:
    day = 5
    realized_round_trip = [
        big_trade("-600", day=day, hour=12, trade_id="big-loss"),
        big_trade("600", day=day, hour=14, trade_id="big-win"),
    ]
    inputs = _approved_inputs()
    inputs["trades"] = [
        t for t in inputs["trades"] if t.closed_at.date() != (T0 + timedelta(days=day)).date()
    ]  # type: ignore[attr-defined, union-attr]
    inputs["trades"] = [*inputs["trades"], *realized_round_trip]  # type: ignore[misc]
    metrics = compute_paper_metrics(**inputs)  # type: ignore[arg-type]
    assert metrics.max_drawdown is not None
    assert metrics.max_drawdown >= D("0.05")  # o ledger realizado chegou a -6% do capital


def test_hourly_curve_catches_the_same_drawdown() -> None:
    inputs = _approved_inputs()
    start = T0 + timedelta(days=5, hours=11)
    curve = [p for p in inputs["curve"] if not (start <= p.at < start + timedelta(days=1))]  # type: ignore[attr-defined, union-attr]
    curve += [
        EquityPoint(at=start + timedelta(hours=1), equity=D(9_400), realized=D(9_400), in_position=False),
        EquityPoint(at=start + timedelta(hours=4), equity=D(10_000), realized=D(10_000), in_position=False),
        EquityPoint(at=start + timedelta(hours=12), equity=D(10_000), realized=D(10_000), in_position=False),
    ]
    inputs["curve"] = curve
    metrics = compute_paper_metrics(**inputs)  # type: ignore[arg-type]
    assert metrics.max_drawdown == D("0.06")
    assert not evaluate_live_gate(metrics, THRESHOLDS).approved


def test_curve_with_a_hole_after_a_legit_stop_is_still_measured() -> None:
    # pausa legitima de 3 dias (stop/start registrados) sem snapshots: o buraco e tolerado
    inputs = _approved_inputs()
    inputs["events"] = [
        event(ControlAction.START, T0),
        event(ControlAction.STOP, T0 + timedelta(days=20)),
        event(ControlAction.START, T0 + timedelta(days=23)),
    ]
    inputs["curve"] = [p for p in inputs["curve"] if not (20 <= (p.at - T0).days < 23)]  # type: ignore[attr-defined, union-attr]
    inputs["max_curve_gap"] = timedelta(hours=2)
    inputs["curve"] = hourly_curve(inputs["curve"])  # type: ignore[arg-type]
    metrics = compute_paper_metrics(**inputs)  # type: ignore[arg-type]
    assert metrics.max_drawdown is not None


def test_curve_with_a_hole_without_a_stop_event_makes_the_drawdown_unmeasured() -> None:
    inputs = _approved_inputs()
    inputs["curve"] = [p for p in inputs["curve"] if not (20 <= (p.at - T0).days < 23)]  # type: ignore[attr-defined, union-attr]
    metrics = compute_paper_metrics(**inputs)  # type: ignore[arg-type]
    assert metrics.max_drawdown is None  # processo morto/snapshots perdidos nao sao pausa legitima


def test_break_even_trade_is_not_counted_as_a_loss_for_the_gate_sample() -> None:
    metrics = compute_paper_metrics(
        initial_equity=D(10_000),
        trades=[
            trade("0", day=1, trade_id="flat"),
            trade("-5", day=2, trade_id="loss"),
            trade("9", day=3, trade_id="win"),
        ],
        curve=[],
        events=[event(ControlAction.START, T0)],
        now=T0 + timedelta(days=5),
    )
    assert (metrics.trades, metrics.losing_trades) == (3, 1)
