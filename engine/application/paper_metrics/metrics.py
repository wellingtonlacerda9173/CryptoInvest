from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from itertools import pairwise

from engine.application.backtest.models import EquityPoint, Record
from engine.application.risk.clock import require_aware
from engine.domain.journal import ACTIVATING_ACTIONS, ControlEvent
from engine.domain.trade import Trade

DAY = timedelta(days=1)
# tolera reinicios/deploys curtos: o dia conta se o robo ficou ativo ao menos esta fracao dele
DEFAULT_MIN_ACTIVE_FRACTION = Decimal("0.9")
# cada snapshot de equity/heartbeat atesta que o processo estava vivo nesta janela seguinte a ele
DEFAULT_LIVENESS_WINDOW = timedelta(hours=2)
# a curva so mede o drawdown se nao tiver buraco maior que isto (nem nas pontas do periodo), salvo buraco
# em que o robo estava desligado por stop/pause registrado (ver `curve_covers`): mesma janela da liveness
DEFAULT_MAX_CURVE_GAP = DEFAULT_LIVENESS_WINDOW
_DRAWDOWN_SCALE = Decimal(1).scaleb(-12)


class PaperMetrics(Record):
    """Metricas do paper. `profit_factor` e None sem perdas (indefinido); `payoff` exige ganhos e perdas.

    `max_drawdown` e o maior entre a queda da curva de equity e a do ledger realizado (inclusive o fundo
    intradiario entre snapshots); e None quando nao pode ser medido (curva vazia ou que nao cobre o periodo).
    """

    operating_days: int
    trades: int
    losing_trades: int
    total_pnl: Decimal
    gross_profit: Decimal
    gross_loss: Decimal
    max_drawdown: Decimal | None
    profit_factor: Decimal | None
    payoff: Decimal | None
    worst_daily_loss: Decimal
    worst_daily_loss_pct: Decimal


def active_intervals(events: Sequence[ControlEvent], now: datetime) -> list[tuple[datetime, datetime]]:
    """Trechos em que o robo esteve ativo: de start/resume/reactivate ate stop/pause/halt (ou `now`)."""
    require_aware(now)
    intervals: list[tuple[datetime, datetime]] = []
    since: datetime | None = None
    for event in sorted(events, key=lambda e: e.at):
        if event.action in ACTIVATING_ACTIONS:
            since = since or event.at
        elif since is not None:
            intervals.append((since, event.at))
            since = None
    if since is not None and since < now:
        intervals.append((since, now))
    return intervals


def alive_intervals(
    timestamps: Sequence[datetime], window: timedelta, now: datetime
) -> list[tuple[datetime, datetime]]:
    """Trechos atestados por snapshots/heartbeats: cada instante vale `window` a partir dele (ate `now`)."""
    merged: list[tuple[datetime, datetime]] = []
    for at in sorted(t for t in timestamps if t < now):
        end = min(at + window, now)
        if merged and at <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((at, end))
    return merged


def intersect(
    left: Sequence[tuple[datetime, datetime]], right: Sequence[tuple[datetime, datetime]]
) -> list[tuple[datetime, datetime]]:
    """Intersecao de duas listas ordenadas de intervalos disjuntos."""
    out: list[tuple[datetime, datetime]] = []
    i = j = 0
    while i < len(left) and j < len(right):
        start, end = max(left[i][0], right[j][0]), min(left[i][1], right[j][1])
        if start < end:
            out.append((start, end))
        if left[i][1] <= right[j][1]:
            i += 1
        else:
            j += 1
    return out


def operating_days(
    events: Sequence[ControlEvent],
    now: datetime,
    min_active_fraction: Decimal = DEFAULT_MIN_ACTIVE_FRACTION,
    *,
    liveness: Sequence[datetime] | None = None,
    liveness_window: timedelta = DEFAULT_LIVENESS_WINDOW,
) -> int:
    """Dias UTC completos (encerrados em `now`) com o robo ativo por ao menos `min_active_fraction` do dia.

    "Ativo" e o que os eventos de controle dizem E o que `liveness` (snapshots de equity e heartbeats)
    comprova: processo morto sem evento `stop` deixa de somar dias assim que cessam as evidencias. Com
    `liveness=None` so os eventos valem (primitiva de baixo nivel; `compute_paper_metrics` sempre informa).
    """
    intervals = active_intervals(events, now)
    if liveness is not None:
        intervals = intersect(intervals, alive_intervals(liveness, liveness_window, now))
    if not intervals:
        return 0
    required = DAY.total_seconds() * float(min_active_fraction)
    first = intervals[0][0].astimezone(UTC).date()
    count = 0
    day = first
    while (end := datetime.combine(day, time(), tzinfo=UTC) + DAY) <= now:
        start = end - DAY
        active = sum(max(0.0, (min(b, end) - max(a, start)).total_seconds()) for a, b in intervals)
        count += active >= required
        day += DAY
    return count


def drawdown(initial_equity: Decimal, curve: Sequence[EquityPoint]) -> Decimal:
    """Queda maxima pico-a-vale da curva (pico inicial = capital inicial), em Decimal com 12 casas."""
    peak, worst = initial_equity, Decimal(0)
    for point in sorted(curve, key=lambda p: p.at):
        peak = max(peak, point.equity)
        if peak > 0:
            worst = max(worst, (peak - point.equity) / peak)
    return worst.quantize(_DRAWDOWN_SCALE, rounding=ROUND_HALF_EVEN)


def ledger_drawdown(initial_equity: Decimal, trades: Sequence[Trade]) -> Decimal:
    """Queda maxima pico-a-vale do ledger realizado: capital inicial + PnL acumulado por ordem de `closed_at`.

    Cada trade e um ponto da curva, entao o fundo intradiario entre dois snapshots aparece mesmo que a
    recuperacao tenha ocorrido antes do proximo.
    """
    equity = peak = initial_equity
    worst = Decimal(0)
    for trade in sorted(trades, key=lambda t: t.closed_at):
        equity += trade.pnl
        peak = max(peak, equity)
        if peak > 0:
            worst = max(worst, (peak - equity) / peak)
    return worst.quantize(_DRAWDOWN_SCALE, rounding=ROUND_HALF_EVEN)


def curve_covers(
    curve: Sequence[EquityPoint],
    start: datetime,
    end: datetime,
    max_gap: timedelta = DEFAULT_MAX_CURVE_GAP,
    *,
    active: Sequence[tuple[datetime, datetime]] | None = None,
) -> bool:
    """A curva vale para medir o drawdown de [start, end]: nao vazia e sem buraco maior que `max_gap`.

    Com `active` (trechos ligados pelos eventos de controle), um buraco maior e tolerado se o robo estava
    desligado nele por stop/pause registrado: so os trechos ativos dentro do buraco precisam caber em
    `max_gap`. Buraco sem stop registrado (processo morto, snapshot perdido) continua invalidando a medicao.
    """
    times = sorted(p.at for p in curve if start <= p.at <= end)
    if not times:
        return False
    edges = [start, *times, end]
    for a, b in pairwise(edges):
        if b - a <= max_gap:
            continue
        if active is None:
            return False
        if any(stop - begin > max_gap for begin, stop in intersect([(a, b)], active)):
            return False
    return True


def profit_factor(pnls: Sequence[Decimal]) -> Decimal | None:
    gains = sum((p for p in pnls if p > 0), Decimal(0))
    losses = -sum((p for p in pnls if p < 0), Decimal(0))
    return gains / losses if losses > 0 else None


def payoff(pnls: Sequence[Decimal]) -> Decimal | None:
    """Ganho medio / perda media (em modulo)."""
    wins = [p for p in pnls if p > 0]
    losses = [-p for p in pnls if p < 0]
    if not wins or not losses:
        return None
    return (sum(wins, Decimal(0)) / len(wins)) / (sum(losses, Decimal(0)) / len(losses))


def worst_daily_loss(initial_equity: Decimal, trades: Sequence[Trade]) -> tuple[Decimal, Decimal]:
    """(maior perda diaria em valor, em fracao do capital no inicio do dia). Dia = data UTC do fechamento.

    A perda do dia e a pior queda intradiaria do ledger (menor PnL acumulado do dia, trade a trade, em
    relacao ao capital do inicio dele), nao so o liquido do fim do dia. O capital do inicio do dia e o
    inicial mais o resultado realizado dos dias anteriores.
    """
    by_day: dict[date, list[Decimal]] = defaultdict(list)
    for trade in sorted(trades, key=lambda t: t.closed_at):
        by_day[trade.closed_at.astimezone(UTC).date()].append(trade.pnl)
    equity, worst, worst_pct = initial_equity, Decimal(0), Decimal(0)
    for day in sorted(by_day):
        running = trough = Decimal(0)
        for pnl in by_day[day]:
            running += pnl
            trough = min(trough, running)
        if trough < 0:
            worst = max(worst, -trough)
            worst_pct = max(worst_pct, -trough / equity) if equity > 0 else worst_pct
        equity += running
    return worst, worst_pct


def compute_paper_metrics(
    *,
    initial_equity: Decimal,
    trades: Sequence[Trade],
    curve: Sequence[EquityPoint],
    events: Sequence[ControlEvent],
    now: datetime,
    min_active_fraction: Decimal = DEFAULT_MIN_ACTIVE_FRACTION,
    heartbeats: Sequence[datetime] = (),
    liveness_window: timedelta = DEFAULT_LIVENESS_WINDOW,
    max_curve_gap: timedelta = DEFAULT_MAX_CURVE_GAP,
) -> PaperMetrics:
    """Os snapshots da curva e os `heartbeats` sao a evidencia de que o processo esteve vivo.

    `max_drawdown` junta a curva e o ledger realizado (o que a curva nao viu entre snapshots); a curva so
    vale se densa (`max_curve_gap`), exceto nos buracos com o robo desligado por stop/pause registrado.
    """
    pnls = [t.pnl for t in trades]
    loss, loss_pct = worst_daily_loss(initial_equity, trades)
    liveness = [*(p.at for p in curve), *heartbeats]
    intervals = active_intervals(events, now)
    measured = bool(intervals) and curve_covers(curve, intervals[0][0], now, max_curve_gap, active=intervals)
    return PaperMetrics(
        operating_days=operating_days(
            events, now, min_active_fraction, liveness=liveness, liveness_window=liveness_window
        ),
        trades=len(trades),
        losing_trades=sum(p < 0 for p in pnls),
        total_pnl=sum(pnls, Decimal(0)),
        gross_profit=sum((p for p in pnls if p > 0), Decimal(0)),
        gross_loss=-sum((p for p in pnls if p < 0), Decimal(0)),
        max_drawdown=(
            max(drawdown(initial_equity, curve), ledger_drawdown(initial_equity, trades))
            if measured
            else None
        ),
        profit_factor=profit_factor(pnls),
        payoff=payoff(pnls),
        worst_daily_loss=loss,
        worst_daily_loss_pct=loss_pct,
    )
