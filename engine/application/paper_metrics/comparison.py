from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal

from engine.application.backtest.models import Record
from engine.application.risk.clock import require_aware
from engine.domain.trade import Trade

# a abertura do paper (latencia, execucao no book) raramente coincide com a do backtest ao segundo
DEFAULT_MATCH_TOLERANCE = timedelta(minutes=10)


class PeriodPerformance(Record):
    initial_equity: Decimal
    trades: tuple[Trade, ...]


class PaperBacktestComparison(Record):
    """Paper x backtest no mesmo periodo. Diferencas = paper - backtest (negativo: o paper rendeu menos)."""

    period_start: datetime
    period_end: datetime
    paper_trades: int
    backtest_trades: int
    trades_diff: int
    paper_pnl: Decimal
    backtest_pnl: Decimal
    pnl_diff: Decimal
    paper_return: Decimal
    backtest_return: Decimal
    return_diff: Decimal
    matched: int
    only_paper: int
    only_backtest: int


def _in_period(trades: Sequence[Trade], start: datetime, end: datetime) -> list[Trade]:
    return sorted((t for t in trades if start <= t.closed_at < end), key=lambda t: (t.opened_at, t.closed_at))


def _ret(pnl: Decimal, initial: Decimal) -> Decimal:
    return pnl / initial if initial > 0 else Decimal(0)


def _match(paper: Sequence[Trade], backtest: Sequence[Trade], tolerance: timedelta) -> int:
    """Pareamento guloso por simbolo e abertura dentro da tolerancia; cada trade do backtest vale uma vez."""
    free = list(backtest)
    matched = 0
    for trade in paper:
        candidates = [
            b for b in free if b.symbol == trade.symbol and abs(b.opened_at - trade.opened_at) <= tolerance
        ]
        if candidates:
            free.remove(min(candidates, key=lambda b: abs(b.closed_at - trade.closed_at)))
            matched += 1
    return matched


def compare_paper_backtest(
    paper: PeriodPerformance,
    backtest: PeriodPerformance,
    *,
    start: datetime,
    end: datetime,
    match_tolerance: timedelta = DEFAULT_MATCH_TOLERANCE,
) -> PaperBacktestComparison:
    """So trades fechados em [start, end); retorno = resultado liquido / capital inicial de cada lado."""
    require_aware(start, "start")
    require_aware(end, "end")
    if end <= start:
        raise ValueError("end must be after start")
    paper_trades = _in_period(paper.trades, start, end)
    backtest_trades = _in_period(backtest.trades, start, end)
    paper_pnl = sum((t.pnl for t in paper_trades), Decimal(0))
    backtest_pnl = sum((t.pnl for t in backtest_trades), Decimal(0))
    paper_return = _ret(paper_pnl, paper.initial_equity)
    backtest_return = _ret(backtest_pnl, backtest.initial_equity)
    matched = _match(paper_trades, backtest_trades, match_tolerance)
    return PaperBacktestComparison(
        period_start=start,
        period_end=end,
        paper_trades=len(paper_trades),
        backtest_trades=len(backtest_trades),
        trades_diff=len(paper_trades) - len(backtest_trades),
        paper_pnl=paper_pnl,
        backtest_pnl=backtest_pnl,
        pnl_diff=paper_pnl - backtest_pnl,
        paper_return=paper_return,
        backtest_return=backtest_return,
        return_diff=paper_return - backtest_return,
        matched=matched,
        only_paper=len(paper_trades) - matched,
        only_backtest=len(backtest_trades) - matched,
    )
