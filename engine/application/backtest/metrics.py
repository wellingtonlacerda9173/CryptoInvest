import math
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from statistics import mean, stdev

from engine.application.backtest.models import (
    BacktestResult,
    DecisionAction,
    DecisionRecord,
    EquityPoint,
    HypotheticalStatus,
    Record,
    RoundTrip,
)
from engine.application.risk import AuditAction, AuditEntry

ANNUALIZATION_DAYS = 365
# o ponto da curva leva `close_time`: a vela das 23:00 fecha a 00:00 mas pertence ao dia que termina
_CANDLE_END = timedelta(microseconds=1)
SHARPE_NOTE = (
    "Sharpe = media/desvio-padrao (amostral) dos retornos DIARIOS (UTC) do equity marcado a mercado, "
    f"anualizado por raiz({ANNUALIZATION_DAYS}) (cripto opera todos os dias), taxa livre de risco 0"
)


class MonthlyResult(Record):
    month: str
    return_pct: float
    realized_pnl: Decimal
    trades: int


class GroupMetrics(Record):
    """Metricas de um conjunto de round-trips (uma estrategia)."""

    name: str
    trades: int
    win_rate: float | None
    pnl: Decimal
    profit_factor: float | None
    expectancy_r: float | None


class Metrics(Record):
    """Ordem de prioridade do projeto: drawdown e consistencia primeiro, lucro depois."""

    max_drawdown: float
    max_drawdown_days: float
    positive_months_ratio: float | None
    worst_month_return: float | None
    total_return: float
    total_pnl: Decimal
    final_equity: Decimal
    trades: int
    wins: int
    losses: int
    breakeven: int
    win_rate: float | None
    profit_factor: float | None
    payoff: float | None
    expectancy_r: float | None
    sharpe: float | None
    idle_pct: float
    fees_paid: Decimal
    monthly: tuple[MonthlyResult, ...]
    by_strategy: tuple[GroupMetrics, ...]


CAPACITY_CODES = frozenset({"position_already_open", "entry_pending", "max_positions", "adding_to_loser"})
# travas de estado do robo (nao sao um juizo sobre o sinal): desligado, pausas e metas diarias
STATE_LOCK_CODES = frozenset(
    {"robot_halted", "paused", "daily_loss_pause", "daily_target_reached", "pair_paused"}
)
HALT_CODE = "robot_halted"
# veto do seletor de estrategias (opt-in): mede o valor do seletor, nao dos filtros de risco
SELECTOR_CODES = frozenset({"selector_veto"})


def veto_kind(code: str) -> str:
    if code in CAPACITY_CODES:
        return "capacity"
    if code in SELECTOR_CODES:
        return "selector"
    return "state_lock" if code in STATE_LOCK_CODES else "filter"


class AvoidedGroup(Record):
    code: str
    kind: str
    signals: int
    simulated: int
    hypothetical_pnl: Decimal
    avoided_losses: Decimal
    forgone_gains: Decimal

    @property
    def net_effect(self) -> Decimal:
        return self.avoided_losses - self.forgone_gains


class AvoidedSummary(Record):
    """Sinais NAO operados e o que teriam rendido, cada um simulado de forma independente.

    Os totais sao a SOMA de simulacoes independentes, nao o efeito sobre o capital: os sinais competiriam
    por posicao e o capital mudaria. `net_effect` > 0: os vetos pouparam dinheiro. Perdas evitadas e ganhos
    perdidos sao somados separados. Os vetos se dividem em capacidade (ja ha posicao/ordem), travas de estado
    (halt, pausas, meta diaria) e filtros reais de mercado/risco; so os filtros medem o valor dos vetos.
    """

    signals: int
    simulated: int
    hypothetical_pnl: Decimal
    avoided_losses: Decimal
    forgone_gains: Decimal
    net_effect: Decimal
    capacity_net_effect: Decimal
    state_locks_net_effect: Decimal
    risk_vetoes_net_effect: Decimal
    halt_at: datetime | None
    halt_signals: int
    halt_simulated: int
    halt_net_effect: Decimal
    hypothetical_win_rate: float | None
    by_code: tuple[AvoidedGroup, ...]


def _ratio(numerator: Decimal, denominator: Decimal) -> float | None:
    return float(numerator / denominator) if denominator != 0 else None


def _profit_factor(trips: Sequence[RoundTrip]) -> float | None:
    gains = sum((t.pnl for t in trips if t.pnl > 0), Decimal(0))
    losses = -sum((t.pnl for t in trips if t.pnl < 0), Decimal(0))
    return _ratio(gains, losses)


def _win_rate(trips: Sequence[RoundTrip]) -> float | None:
    return sum(1 for t in trips if t.pnl > 0) / len(trips) if trips else None


def _expectancy(trips: Sequence[RoundTrip]) -> float | None:
    return float(sum((t.r_multiple for t in trips), Decimal(0)) / len(trips)) if trips else None


def max_drawdown(initial: Decimal, curve: Sequence[EquityPoint]) -> tuple[float, float]:
    """(drawdown maximo como fracao do pico, maior tempo submerso em dias) da curva marcada a mercado."""
    peak, peak_at = initial, curve[0].at if curve else None
    worst = Decimal(0)
    longest = timedelta(0)
    for point in curve:
        if point.equity >= peak:
            if peak_at is not None:
                longest = max(longest, point.at - peak_at)
            peak, peak_at = point.equity, point.at
        elif peak > 0:
            worst = max(worst, (peak - point.equity) / peak)
    if curve and peak_at is not None and curve[-1].equity < peak:
        longest = max(longest, curve[-1].at - peak_at)
    return float(worst), longest.total_seconds() / 86_400


def daily_returns(initial: Decimal, curve: Sequence[EquityPoint]) -> list[float]:
    last_by_day: dict[object, Decimal] = {}
    for point in curve:
        last_by_day[(point.at - _CANDLE_END).date()] = point.equity
    previous, returns = initial, []
    for equity in last_by_day.values():
        returns.append(float((equity - previous) / previous) if previous > 0 else 0.0)
        previous = equity
    return returns


def sharpe_ratio(returns: Sequence[float]) -> float | None:
    if len(returns) < 2:
        return None
    deviation = stdev(returns)
    return mean(returns) / deviation * math.sqrt(ANNUALIZATION_DAYS) if deviation > 0 else None


def _month(moment: datetime) -> str:
    return f"{moment.year:04d}-{moment.month:02d}"


def monthly_results(
    initial: Decimal, curve: Sequence[EquityPoint], trips: Sequence[RoundTrip]
) -> tuple[MonthlyResult, ...]:
    end_equity: dict[str, Decimal] = {}
    for point in curve:
        end_equity[_month(point.at - _CANDLE_END)] = point.equity
    pnl: dict[str, Decimal] = defaultdict(Decimal)
    count: dict[str, int] = defaultdict(int)
    for trip in trips:
        pnl[_month(trip.attributed_at)] += trip.pnl
        count[_month(trip.attributed_at)] += 1
    previous, rows = initial, []
    for month, equity in end_equity.items():
        rows.append(
            MonthlyResult(
                month=month,
                return_pct=float((equity - previous) / previous) if previous > 0 else 0.0,
                realized_pnl=pnl[month],
                trades=count[month],
            )
        )
        previous = equity
    return tuple(rows)


def metrics_from_parts(initial: Decimal, curve: Sequence[EquityPoint], trips: Sequence[RoundTrip]) -> Metrics:
    final = curve[-1].equity if curve else initial
    drawdown, drawdown_days = max_drawdown(initial, curve)
    monthly = monthly_results(initial, curve, trips)
    wins = [t for t in trips if t.pnl > 0]
    losses = [t for t in trips if t.pnl < 0]
    avg_win = sum((t.pnl for t in wins), Decimal(0)) / len(wins) if wins else None
    avg_loss = -sum((t.pnl for t in losses), Decimal(0)) / len(losses) if losses else None
    names = sorted({t.strategy for t in trips})
    return Metrics(
        max_drawdown=drawdown,
        max_drawdown_days=drawdown_days,
        positive_months_ratio=(
            sum(1 for m in monthly if m.return_pct > 0) / len(monthly) if monthly else None
        ),
        worst_month_return=min((m.return_pct for m in monthly), default=None),
        total_return=float((final - initial) / initial) if initial > 0 else 0.0,
        total_pnl=final - initial,
        final_equity=final,
        trades=len(trips),
        wins=len(wins),
        losses=len(losses),
        breakeven=len(trips) - len(wins) - len(losses),
        win_rate=_win_rate(trips),
        profit_factor=_profit_factor(trips),
        payoff=_ratio(avg_win, avg_loss) if avg_win is not None and avg_loss is not None else None,
        expectancy_r=_expectancy(trips),
        sharpe=sharpe_ratio(daily_returns(initial, curve)),
        idle_pct=sum(1 for p in curve if not p.in_position) / len(curve) if curve else 1.0,
        fees_paid=sum((t.fees for t in trips), Decimal(0)),
        monthly=monthly,
        by_strategy=tuple(
            GroupMetrics(
                name=name,
                trades=len(group),
                win_rate=_win_rate(group),
                pnl=sum((t.pnl for t in group), Decimal(0)),
                profit_factor=_profit_factor(group),
                expectancy_r=_expectancy(group),
            )
            for name in names
            if (group := [t for t in trips if t.strategy == name])
        ),
    )


def compute_metrics(result: BacktestResult) -> Metrics:
    return metrics_from_parts(result.initial_capital, result.equity_curve, result.round_trips)


_FILLED = frozenset({HypotheticalStatus.TARGET, HypotheticalStatus.STOP, HypotheticalStatus.TIMEOUT})


def _group(code: str, items: Sequence[DecisionRecord]) -> AvoidedGroup:
    done = [d.hypothetical.pnl for d in items if d.hypothetical and d.hypothetical.status in _FILLED]
    return AvoidedGroup(
        code=code,
        kind=veto_kind(code),
        signals=len(items),
        simulated=len(done),
        hypothetical_pnl=sum(done, Decimal(0)),
        avoided_losses=-sum((p for p in done if p < 0), Decimal(0)),
        forgone_gains=sum((p for p in done if p > 0), Decimal(0)),
    )


def halt_time(events: Sequence[AuditEntry]) -> datetime | None:
    return next((e.at for e in events if e.action is AuditAction.HALTED), None)


def summarize_avoided(
    decisions: Sequence[DecisionRecord], risk_events: Sequence[AuditEntry] = ()
) -> AvoidedSummary:
    vetoed = [d for d in decisions if d.action is DecisionAction.SKIPPED and d.hypothetical is not None]
    by_code: dict[str, list[DecisionRecord]] = defaultdict(list)
    for decision in vetoed:
        by_code[decision.code].append(decision)
    groups = [_group(code, items) for code, items in sorted(by_code.items())]
    total = _group("total", vetoed)
    filled = [d.hypothetical for d in vetoed if d.hypothetical and d.hypothetical.status in _FILLED]
    halt = _group(HALT_CODE, by_code.get(HALT_CODE, []))

    def net(kind: str) -> Decimal:
        return sum((g.net_effect for g in groups if g.kind == kind), Decimal(0))

    return AvoidedSummary(
        signals=total.signals,
        simulated=total.simulated,
        hypothetical_pnl=total.hypothetical_pnl,
        avoided_losses=total.avoided_losses,
        forgone_gains=total.forgone_gains,
        net_effect=total.net_effect,
        capacity_net_effect=net("capacity"),
        state_locks_net_effect=net("state_lock"),
        risk_vetoes_net_effect=net("filter"),
        halt_at=halt_time(risk_events),
        halt_signals=halt.signals,
        halt_simulated=halt.simulated,
        halt_net_effect=halt.net_effect,
        hypothetical_win_rate=sum(1 for h in filled if h.pnl > 0) / len(filled) if filled else None,
        by_code=tuple(groups),
    )


def compute_active_metrics(result: BacktestResult) -> Metrics | None:
    """Metricas so do trecho anterior ao halt (None se o robo nunca foi desligado)."""
    if (halted_at := halt_time(result.risk_events)) is None:
        return None
    curve = [p for p in result.equity_curve if p.at - _CANDLE_END <= halted_at]
    trips = [t for t in result.round_trips if t.attributed_at <= halted_at]
    return metrics_from_parts(result.initial_capital, curve, trips)
