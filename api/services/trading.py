"""Leituras de trading para o painel (saldo, equity, posições, operações, decisões, desempenho, velas)."""

import asyncio
import csv
import io
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Literal, cast

from sqlalchemy import Select, case, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from api.container import Container
from api.reasons import EXIT_REASONS, GLOSSARY, REASONS, STATES, translate
from api.schemas_trading import (
    AccountSummaryOut,
    AvoidedOut,
    CandleOut,
    CandlesOut,
    DecisionOut,
    DecisionPage,
    DecisionSummaryOut,
    EquityPointOut,
    EquitySeriesOut,
    GlossaryItemOut,
    GlossaryOut,
    GroupStatsOut,
    LevelOut,
    MarkerOut,
    Mode,
    PerformanceOut,
    PositionOut,
    ReasonWordOut,
    StateWordOut,
    TradeOut,
    TradePage,
    TradesSummaryOut,
)
from api.services.livegate import LiveGateService
from api.services.perf_math import daily_returns, idle_fraction, profit_factor_of, sharpe
from api.services.queries import (
    equity_at,
    first_equity,
    last_equity,
    latest_run,
    open_position_rows,
)
from api.services.runner_status import RunnerSnapshot, RunnerStatusService
from engine.application.detectors import ElephantBarConfig, detect_elephant_bar
from engine.application.paper_metrics import drawdown, ledger_drawdown
from engine.domain.candle import Candle
from engine.domain.types import Timeframe
from engine.infrastructure.persistence.config_repository import ConfigRepository
from engine.infrastructure.persistence.models import (
    AuditLogRow,
    CandleRow,
    DecisionRow,
    PositionRow,
    RunRow,
    TradeRow,
)

MAX_EXPORT_ROWS = 20_000
SMALL_SAMPLE = 30
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
MODE_LABEL = {"paper": "PAPER (simulado)", "live": "LIVE (dinheiro real)", "backtest": "BACKTEST"}


def _f(value: Decimal | float | None) -> float | None:
    return None if value is None else float(value)


def safe_cell(value: object) -> str:
    """CSV: texto que começaria com `=`, `+`, `-`, `@` vira texto literal (evita fórmula em planilhas)."""
    cell = "" if value is None else str(value)
    return "'" + cell if cell.startswith(_FORMULA_PREFIXES) else cell


async def stored_capital(db: AsyncSession, run_id: str) -> tuple[Decimal | None, Timeframe]:
    """Capital inicial e tempo gráfico gravados no início do run (`paper_start` do audit_log)."""
    stmt = (
        select(AuditLogRow.payload)
        .where(AuditLogRow.entity == run_id, AuditLogRow.action == "paper_start")
        .order_by(AuditLogRow.id)
        .limit(1)
    )
    payload = await db.scalar(stmt)
    config = ((payload or {}).get("config") or {}).get("config") or {}
    capital = Decimal(str(config["capital"])) if config.get("capital") is not None else None
    try:
        timeframe = Timeframe(config.get("timeframe", "1h"))
    except ValueError:
        timeframe = Timeframe.H1
    return capital, timeframe


def _mode(row_mode: str) -> Mode:
    return cast("Mode", row_mode)


@dataclass(frozen=True, slots=True)
class TradeFilter:
    """Filtros das operações (o modo é sempre um só: totais de modos diferentes nunca se misturam)."""

    mode: str = "paper"
    run_id: str | None = None
    symbol: str | None = None
    strategy: str | None = None
    exit_reason: str | None = None
    result: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    q: str | None = None


class TradingReads:
    def __init__(self, container: Container) -> None:
        self._c = container

    async def _snapshot(self) -> RunnerSnapshot:
        return await RunnerStatusService(self._c).snapshot()

    async def _pause_hours(self) -> int | None:
        """Pausa do par configurada: a do robô em execução ou, sem ele, a da última versão salva."""
        data = (await self._snapshot()).data
        risk = None if not data else data.get("risk")
        hours = risk.get("pair_pause_hours") if isinstance(risk, dict) else None
        if isinstance(hours, int) and not isinstance(hours, bool):
            return hours
        async with self._c.sessions() as db:
            latest = await ConfigRepository(db).latest()
        return None if latest is None else latest.document.risk.pair_pause_hours

    # ------------------------------------------------------------------ saldo e P&L

    async def account(self) -> AccountSummaryOut:
        snapshot = await self._snapshot()
        now = self._c.clock()
        async with self._c.sessions() as db:
            run = await latest_run(db, snapshot.run_id)
            if run is None:
                return AccountSummaryOut(
                    mode="paper",
                    simulated=True,
                    currency="USDT",
                    initial_capital=None,
                    equity=None,
                    realized_equity=None,
                    day_pnl=None,
                    day_pnl_pct=None,
                    week_pnl=None,
                    week_pnl_pct=None,
                    total_pnl=None,
                    total_pnl_pct=None,
                    as_of=None,
                )
            capital, _ = await stored_capital(db, run.run_id)
            initial = capital or await first_equity(db, run.run_id)
            last = await last_equity(db, run.run_id)
            data = snapshot.data if snapshot.data and snapshot.run_id == run.run_id else None
            published = _published(data, "equity")
            equity = published if published is not None else (last.equity if last else initial)
            published_realized = _published(data, "realized_equity")
            realized = (
                published_realized if published_realized is not None else (last.realized if last else initial)
            )
            day_start = datetime.combine(now.astimezone(UTC).date(), time(), tzinfo=UTC)
            week_start = day_start - timedelta(days=day_start.weekday())
            base_day = await equity_at(db, run.run_id, day_start) or initial
            base_week = await equity_at(db, run.run_id, week_start) or initial
        return AccountSummaryOut(
            mode="paper",
            simulated=True,
            currency="USDT",
            initial_capital=_f(initial),
            equity=_f(equity),
            realized_equity=_f(realized),
            day_pnl=_diff(equity, base_day),
            day_pnl_pct=_pct(equity, base_day),
            week_pnl=_diff(equity, base_week),
            week_pnl_pct=_pct(equity, base_week),
            total_pnl=_diff(equity, initial),
            total_pnl_pct=_pct(equity, initial),
            as_of=snapshot.at or (last.at if last else None),
        )

    async def equity_series(self, *, days: int, points: int) -> EquitySeriesOut:
        now = self._c.clock()
        async with self._c.sessions() as db:
            run = await latest_run(db)
            if run is None:
                return EquitySeriesOut(mode="paper", simulated=True, initial_capital=None, points=[])
            capital, _ = await stored_capital(db, run.run_id)
            since = max(now - timedelta(days=days), run.started_at - timedelta(minutes=1))
            step = max(60.0, (now - since).total_seconds() / max(points, 2))
            rows = (
                await db.execute(
                    text(
                        "SELECT DISTINCT ON (b) at, equity, realized FROM ("
                        " SELECT date_bin(make_interval(secs => CAST(:step AS double precision)), at,"
                        "  TIMESTAMPTZ '2000-01-01 00:00:00+00') AS b, at, equity, realized"
                        " FROM equity_snapshots WHERE run_id = :run AND at >= :since) s"
                        " ORDER BY b, at DESC"
                    ),
                    {"step": step, "run": run.run_id, "since": since},
                )
            ).all()
        return EquitySeriesOut(
            mode="paper",
            simulated=True,
            initial_capital=_f(capital),
            points=[
                EquityPointOut(at=r.at, equity=float(r.equity), realized=float(r.realized)) for r in rows
            ],
        )

    # ------------------------------------------------------------------ posições

    async def positions(self) -> list[PositionOut]:
        snapshot = await self._snapshot()
        async with self._c.sessions() as db:
            run = await latest_run(db, snapshot.run_id)
            if run is None:
                return []
            rows = await open_position_rows(db, run.run_id)
            _, timeframe = await stored_capital(db, run.run_id)
            prices = {r.symbol: await _last_close(db, r.symbol, timeframe) for r in rows}
        blind = bool(snapshot.data and snapshot.data.get("stops_blind"))
        watched = snapshot.alive and not blind
        note = (
            "Stop ativo"
            if watched
            else "Sem dados de mercado: o stop está salvo, mas só age quando os preços voltarem."
            if blind
            else "Robô sem sinal de vida: o stop não está sendo acompanhado agora."
        )
        return [_position(r, run.run_id, prices[r.symbol], watched, note) for r in rows]

    # ------------------------------------------------------------------ operações

    def _trade_query(self, base: Any, f: "TradeFilter") -> Any:
        stmt = base.join(RunRow, RunRow.run_id == TradeRow.run_id).where(RunRow.mode == f.mode)
        if f.run_id:
            stmt = stmt.where(TradeRow.run_id == f.run_id)
        if f.symbol:
            stmt = stmt.where(TradeRow.symbol == f.symbol)
        if f.strategy:
            stmt = stmt.where(TradeRow.strategy == f.strategy)
        if f.exit_reason:
            stmt = stmt.where(TradeRow.exit_reason == f.exit_reason)
        if f.result == "win":
            stmt = stmt.where(TradeRow.pnl > 0)
        elif f.result == "loss":
            stmt = stmt.where(TradeRow.pnl < 0)
        elif f.result == "breakeven":
            stmt = stmt.where(TradeRow.pnl == 0)
        if f.since:
            stmt = stmt.where(TradeRow.closed_at >= f.since)
        if f.until:
            stmt = stmt.where(TradeRow.closed_at < f.until)
        if f.q:
            stmt = stmt.where(
                TradeRow.symbol.icontains(f.q, autoescape=True)
                | TradeRow.strategy.icontains(f.q, autoescape=True)
                | TradeRow.trade_id.icontains(f.q, autoescape=True)
                | TradeRow.exit_reason.icontains(f.q, autoescape=True)
            )
        return stmt

    async def trades(self, f: "TradeFilter", *, limit: int, cursor: int | None) -> TradePage:
        stmt = self._trade_query(select(TradeRow, RunRow.mode), f).order_by(TradeRow.id.desc())
        if cursor is not None:
            stmt = stmt.where(TradeRow.id < cursor)
        totals = self._trade_query(
            select(
                func.count(),
                func.coalesce(func.sum(case((TradeRow.pnl > 0, 1), else_=0)), 0),
                func.coalesce(func.sum(case((TradeRow.pnl < 0, 1), else_=0)), 0),
                func.coalesce(func.sum(TradeRow.pnl), 0),
                func.coalesce(func.sum(TradeRow.entry_fee + TradeRow.exit_fee), 0),
            ).select_from(TradeRow),
            f,
        )
        async with self._c.sessions() as db:
            rows = (await db.execute(stmt.limit(limit + 1))).all()
            count, wins, losses, pnl, fees = (await db.execute(totals)).one()
        page = rows[:limit]
        return TradePage(
            items=[_trade(row, row_mode) for row, row_mode in page],
            next_cursor=page[-1][0].id if len(rows) > limit else None,
            summary=TradesSummaryOut(
                count=int(count), wins=int(wins), losses=int(losses), pnl=float(pnl), fees=float(fees)
            ),
        )

    async def trades_csv(self, f: "TradeFilter") -> tuple[str, str]:
        mode = f.mode
        stmt = self._trade_query(select(TradeRow, RunRow.mode), f).order_by(TradeRow.id.desc())
        async with self._c.sessions() as db:
            rows = (await db.execute(stmt.limit(MAX_EXPORT_ROWS + 1))).all()
        truncated = len(rows) > MAX_EXPORT_ROWS
        buffer = io.StringIO()
        buffer.write(f"# CryptoInvest - operações encerradas - modo {MODE_LABEL.get(mode, mode)}\r\n")
        buffer.write(f"# gerado em {self._c.clock():%Y-%m-%d %H:%M:%S} UTC\r\n")
        if truncated:
            buffer.write(f"# ATENÇÃO: limitado às {MAX_EXPORT_ROWS} operações mais recentes\r\n")
        writer = csv.writer(buffer, lineterminator="\r\n")
        writer.writerow(
            [
                "modo",
                "run",
                "id",
                "par",
                "estrategia",
                "abertura_utc",
                "fechamento_utc",
                "entrada",
                "saida",
                "quantidade",
                "resultado_usdt",
                "taxas_usdt",
                "resultado_r",
                "motivo_saida",
            ]
        )
        for row, row_mode in rows[:MAX_EXPORT_ROWS]:
            writer.writerow(
                [
                    mode_label_short(row_mode),
                    safe_cell(row.run_id),
                    safe_cell(row.trade_id),
                    safe_cell(row.symbol),
                    safe_cell(row.strategy),
                    row.opened_at.astimezone(UTC).isoformat(),
                    row.closed_at.astimezone(UTC).isoformat(),
                    format(row.entry_price.normalize(), "f"),
                    format(row.exit_price.normalize(), "f"),
                    format(row.quantity.normalize(), "f"),
                    format(row.pnl.normalize(), "f"),
                    format((row.entry_fee + row.exit_fee).normalize(), "f"),
                    "" if row.r_multiple is None else format(row.r_multiple.normalize(), "f"),
                    safe_cell(row.exit_reason),
                ]
            )
        filename = f"operacoes-{mode}-{self._c.clock():%Y%m%d}.csv"
        return buffer.getvalue(), filename

    # ------------------------------------------------------------------ decisões

    async def decisions(
        self,
        *,
        mode: str,
        limit: int,
        cursor: int | None,
        run_id: str | None,
        traded: bool | None,
        symbol: str | None,
        code: str | None,
        since: datetime | None,
    ) -> DecisionPage:
        stmt = (
            select(DecisionRow, RunRow.mode)
            .join(RunRow, RunRow.run_id == DecisionRow.run_id)
            .where(RunRow.mode == mode)
            .order_by(DecisionRow.id.desc())
        )
        if run_id:
            stmt = stmt.where(DecisionRow.run_id == run_id)
        if traded is not None:
            stmt = stmt.where(DecisionRow.traded.is_(traded))
        if symbol:
            stmt = stmt.where(DecisionRow.symbol == symbol)
        if code:
            stmt = stmt.where(DecisionRow.code == code)
        if since:
            stmt = stmt.where(DecisionRow.decided_at >= since)
        if cursor is not None:
            stmt = stmt.where(DecisionRow.id < cursor)
        hours = await self._pause_hours()
        async with self._c.sessions() as db:
            rows = (await db.execute(stmt.limit(limit + 1))).all()
            summary = await self._decision_summary(db, mode, run_id) if cursor is None else None
        page = rows[:limit]
        return DecisionPage(
            items=[_decision(row, row_mode, hours) for row, row_mode in page],
            next_cursor=page[-1][0].id if len(rows) > limit else None,
            summary=summary,
        )

    async def _decision_summary(self, db: AsyncSession, mode: str, run_id: str | None) -> DecisionSummaryOut:
        today = self._c.clock().astimezone(UTC).date()
        start = datetime.combine(today, time(), tzinfo=UTC)
        stmt = (
            select(
                func.coalesce(func.sum(case((DecisionRow.traded.is_(True), 1), else_=0)), 0),
                func.coalesce(
                    func.sum(
                        case(
                            ((DecisionRow.traded.is_(False)) & DecisionRow.signal_id.is_not(None), 1), else_=0
                        )
                    ),
                    0,
                ),
                func.coalesce(func.sum(case((DecisionRow.signal_id.is_(None), 1), else_=0)), 0),
            )
            .select_from(DecisionRow)
            .join(RunRow, RunRow.run_id == DecisionRow.run_id)
            .where(RunRow.mode == mode, DecisionRow.decided_at >= start)
        )
        if run_id:
            stmt = stmt.where(DecisionRow.run_id == run_id)
        entries, refusals, waits = (await db.execute(stmt)).one()
        return DecisionSummaryOut(
            day=today,
            signals=int(entries) + int(refusals),
            entries=int(entries),
            refusals=int(refusals),
            waits=int(waits),
        )

    # ------------------------------------------------------------------ desempenho

    async def performance(self, *, days: int | None, run_id: str | None) -> PerformanceOut:
        snapshot = await self._snapshot()
        now = self._c.clock()
        async with self._c.sessions() as db:
            run = await latest_run(db, run_id or snapshot.run_id)
        if run is None:
            return _empty_performance(now)
        evaluation = await LiveGateService(self._c).evaluate(run.run_id)
        inputs = evaluation.inputs
        capital = inputs.capital if inputs is not None else Decimal(0)
        since = None if days is None else now - timedelta(days=days)
        period_start = max(run.started_at, since) if since else run.started_at
        hours = await self._pause_hours()
        async with self._c.sessions() as db:
            groups = await _group_stats(db, run.run_id, since)
            avoided = await _avoided(db, run.run_id, since, hours)
            intervals = await _position_intervals(db, run.run_id)
        stats = groups["all"]
        trades_in = [t for t in (inputs.trades if inputs else []) if since is None or t.closed_at >= since]
        curve = [p for p in (inputs.curve if inputs else []) if since is None or p.at >= since]
        dd_values = [
            drawdown(capital, curve) if curve else None,
            ledger_drawdown(capital, trades_in) if trades_in else None,
        ]
        measured = [v for v in dd_values if v is not None]
        wins, losses = stats["wins"], stats["losses"]
        pnl = stats["pnl"]
        by_month = groups["month"]
        positive = sum(1 for g in by_month if g.pnl > 0)
        refused = sum(a.count for a in avoided)
        trades = wins + losses + stats["even"]
        avg_win = stats["gross_profit"] / wins if wins else None
        avg_loss = stats["gross_loss"] / losses if losses else None
        warning = (
            f"{trades} operações: poucas para concluir qualquer coisa (o ideal são {SMALL_SAMPLE} ou mais)."
            if trades < SMALL_SAMPLE
            else None
        )
        notes = ["Ficar parado em USDT é uma decisão válida: não é falha do robô."]
        if inputs is not None and inputs.allow_open_calendar:
            notes.append(
                "Este teste rodou sem o calendário de eventos e não vale como evidência para o Live."
            )
        return PerformanceOut(
            run_id=run.run_id,
            mode="paper",
            simulated=True,
            period_start=period_start,
            period_end=now,
            trades=trades,
            wins=wins,
            losses=losses,
            win_rate=None if trades == 0 else round(wins / trades, 4),
            profit_factor=profit_factor_of(stats["gross_profit"], stats["gross_loss"]),
            payoff=None if not avg_win or not avg_loss else float(avg_win / avg_loss),
            expectancy_r=_f(stats["expectancy_r"]),
            total_pnl=float(pnl),
            total_return_pct=None if capital <= 0 else round(float(pnl / capital * 100), 4),
            max_drawdown_pct=None if not measured else round(float(max(measured)) * 100, 4),
            sharpe=_round(sharpe(daily_returns(curve, capital)) if capital > 0 else None),
            idle_pct=_round(idle_fraction(intervals, period_start, now), 4, scale=100),
            positive_months=positive,
            months=len(by_month),
            by_strategy=[_group_out(g) for g in groups["strategy"]],
            by_pair=[_group_out(g) for g in groups["pair"]],
            by_month=[_group_out(g) for g in by_month],
            avoided=avoided,
            signals_refused=refused,
            sample_warning=warning,
            notes=notes,
        )

    @staticmethod
    def _last_decision_text(decision: DecisionRow, pause_hours: int | None) -> str:
        reason = translate(decision.code, traded=decision.traded, pair_pause_hours=pause_hours)
        return f"{reason.label}: {reason.text}"

    # ------------------------------------------------------------------ mercado

    async def candles(self, *, symbol: str, timeframe: Timeframe, limit: int) -> CandlesOut:
        snapshot = await self._snapshot()
        warmup = ElephantBarConfig().min_history + 5
        async with self._c.sessions() as db:
            rows = list(
                (
                    await db.scalars(
                        select(CandleRow)
                        .where(CandleRow.symbol == symbol, CandleRow.timeframe == timeframe.value)
                        .order_by(CandleRow.open_time.desc())
                        .limit(limit + warmup)
                    )
                ).all()
            )[::-1]
            latest = await ConfigRepository(db).latest()
            run = await latest_run(db, snapshot.run_id)
            trade_rows: list[TradeRow] = []
            positions: list[PositionRow] = []
            if run is not None and rows:
                trade_rows = list(
                    (
                        await db.scalars(
                            select(TradeRow)
                            .where(TradeRow.run_id == run.run_id, TradeRow.symbol == symbol)
                            .where(TradeRow.closed_at >= rows[0].open_time)
                            .order_by(TradeRow.closed_at)
                        )
                    ).all()
                )
                positions = [p for p in await open_position_rows(db, run.run_id) if p.symbol == symbol]
            decision = await db.scalar(
                select(DecisionRow)
                .where(DecisionRow.symbol == symbol)
                .order_by(DecisionRow.id.desc())
                .limit(1)
            )
        shown = rows[warmup:] if len(rows) > limit else rows
        config = latest.document.strategies.elephant.detector if latest else ElephantBarConfig()
        domain = [_candle(r, timeframe) for r in rows]
        marks = await asyncio.to_thread(_elephants, domain, shown[0].open_time if shown else None, config)
        markers = marks + _trade_markers(trade_rows)
        last_price = _f(rows[-1].close) if rows else None
        change, volume = _day_stats(rows, timeframe)
        return CandlesOut(
            symbol=symbol,
            timeframe=timeframe.value,
            mode="paper",
            simulated=True,
            candles=[
                CandleOut(
                    t=r.open_time,
                    o=float(r.open),
                    h=float(r.high),
                    l=float(r.low),
                    c=float(r.close),
                    v=float(r.volume),
                )
                for r in shown
            ],
            markers=sorted(markers, key=lambda m: m.t),
            levels=[lv for p in positions for lv in _levels(p)],
            last_price=last_price,
            change_24h_pct=change,
            volume_24h=volume,
            last_decision=None
            if decision is None
            else self._last_decision_text(
                decision, None if latest is None else latest.document.risk.pair_pause_hours
            ),
        )

    # ------------------------------------------------------------------ glossário

    @staticmethod
    def glossary() -> GlossaryOut:
        return GlossaryOut(
            terms=[GlossaryItemOut(term=g.term, short=g.short, long=g.long) for g in GLOSSARY],
            states=[StateWordOut(state=k, label=v[0], detail=v[1]) for k, v in STATES.items()],
            reasons=[ReasonWordOut(code=k, label=v.label, text=v.text) for k, v in REASONS.items()],
        )


# ---------------------------------------------------------------------- auxiliares


def mode_label_short(mode: str) -> str:
    return mode.upper()


def _diff(equity: Decimal | None, base: Decimal | None) -> float | None:
    return None if equity is None or base is None else float(equity - base)


def _pct(equity: Decimal | None, base: Decimal | None) -> float | None:
    if equity is None or base is None or base <= 0:
        return None
    return round(float((equity - base) / base * 100), 4)


def _round(value: float | None, places: int = 3, *, scale: float = 1.0) -> float | None:
    return None if value is None else round(value * scale, places)


async def _last_close(db: AsyncSession, symbol: str, timeframe: Timeframe) -> Decimal | None:
    stmt = (
        select(CandleRow.close)
        .where(CandleRow.symbol == symbol, CandleRow.timeframe == timeframe.value)
        .order_by(CandleRow.open_time.desc())
        .limit(1)
    )
    return await db.scalar(stmt)


def _position(row: PositionRow, run_id: str, last: Decimal | None, watched: bool, note: str) -> PositionOut:
    risk = (row.entry_price - row.initial_stop) * row.initial_quantity
    unrealized = None if last is None else (last - row.entry_price) * row.quantity + row.realized_pnl
    return PositionOut(
        run_id=run_id,
        mode="paper",
        symbol=row.symbol,
        quantity=float(row.quantity),
        entry_price=float(row.entry_price),
        stop=float(row.stop),
        initial_stop=float(row.initial_stop),
        take_profit=_f(row.take_profit),
        last_price=_f(last),
        unrealized_pnl=_f(unrealized),
        unrealized_r=None if unrealized is None or risk <= 0 else round(float(unrealized / risk), 3),
        risk_amount=float(risk),
        opened_at=row.opened_at,
        breakeven_reached=row.breakeven_reached,
        partial_done=row.partial_done,
        protected=watched,
        protection_note=note,
    )


def _trade(row: TradeRow, mode: str) -> TradeOut:
    result: Literal["win", "loss", "breakeven"] = (
        "win" if row.pnl > 0 else "loss" if row.pnl < 0 else "breakeven"
    )
    return TradeOut(
        id=row.id,
        trade_id=row.trade_id,
        run_id=row.run_id,
        mode=_mode(mode),
        symbol=row.symbol,
        strategy=row.strategy,
        opened_at=row.opened_at,
        closed_at=row.closed_at,
        entry_price=float(row.entry_price),
        exit_price=float(row.exit_price),
        quantity=float(row.quantity),
        pnl=float(row.pnl),
        fees=float(row.entry_fee + row.exit_fee),
        r_multiple=_f(row.r_multiple),
        exit_reason=row.exit_reason,
        exit_reason_text=EXIT_REASONS.get(row.exit_reason, row.exit_reason),
        result=result,
    )


def _published(data: dict[str, Any] | None, key: str) -> Decimal | None:
    """Valor publicado pelo robô, inclusive zero (`0.0` é um saldo real, não 'ausente'); lixo = ausente."""
    raw = None if data is None else data.get(key)
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = Decimal(str(raw))
    except ArithmeticError:
        return None
    return value if value.is_finite() else None


def _decision(row: DecisionRow, mode: str, pause_hours: int | None = None) -> DecisionOut:
    reason = translate(row.code, traded=row.traded, pair_pause_hours=pause_hours)
    label = "Entrou" if row.traded else reason.label
    return DecisionOut(
        id=row.id,
        run_id=row.run_id,
        mode=_mode(mode),
        at=row.decided_at,
        symbol=row.symbol,
        action=cast("Literal['enter', 'exit', 'hold', 'skip']", row.action),
        traded=row.traded,
        outcome=label,
        outcome_kind="entered" if row.traded else reason.kind,
        code=row.code,
        reason_text=reason.text,
        detail=row.reason,
        strategy=row.strategy,
        confidence=row.confidence,
        signal=row.signal,
    )


class _Group:
    def __init__(
        self, key: str, trades: int, wins: int, losses: int, pnl: Decimal, gp: Decimal, gl: Decimal
    ) -> None:
        self.key, self.trades, self.wins, self.losses = key, trades, wins, losses
        self.pnl, self.gross_profit, self.gross_loss = pnl, gp, gl


def _group_out(g: _Group) -> GroupStatsOut:
    return GroupStatsOut(
        key=g.key,
        trades=g.trades,
        wins=g.wins,
        pnl=float(g.pnl),
        win_rate=None if g.trades == 0 else round(g.wins / g.trades, 4),
        profit_factor=profit_factor_of(g.gross_profit, g.gross_loss),
    )


async def _group_stats(db: AsyncSession, run_id: str, since: datetime | None) -> dict[str, Any]:
    def aggregate(key: Any) -> Select[Any]:
        stmt = (
            select(
                key.label("k"),
                func.count(),
                func.sum(case((TradeRow.pnl > 0, 1), else_=0)),
                func.sum(case((TradeRow.pnl < 0, 1), else_=0)),
                func.sum(TradeRow.pnl),
                func.coalesce(func.sum(case((TradeRow.pnl > 0, TradeRow.pnl), else_=0)), 0),
                func.coalesce(func.sum(case((TradeRow.pnl < 0, -TradeRow.pnl), else_=0)), 0),
            )
            .where(TradeRow.run_id == run_id)
            .group_by(text("k"))
            .order_by(text("k"))
        )
        return stmt.where(TradeRow.closed_at >= since) if since else stmt

    async def run(key: Any, label: str = "") -> list[_Group]:
        rows = (await db.execute(aggregate(key))).all()
        return [
            _Group(
                str(r[0] if r[0] is not None else "sem estratégia"),
                int(r[1]),
                int(r[2]),
                int(r[3]),
                r[4],
                r[5],
                r[6],
            )
            for r in rows
        ]

    month = func.to_char(func.timezone("UTC", TradeRow.closed_at), "YYYY-MM")
    by_strategy = await run(TradeRow.strategy)
    by_pair = await run(TradeRow.symbol)
    by_month = await run(month)
    total = select(
        func.count(),
        func.coalesce(func.sum(case((TradeRow.pnl > 0, 1), else_=0)), 0),
        func.coalesce(func.sum(case((TradeRow.pnl < 0, 1), else_=0)), 0),
        func.coalesce(func.sum(case((TradeRow.pnl == 0, 1), else_=0)), 0),
        func.coalesce(func.sum(TradeRow.pnl), 0),
        func.coalesce(func.sum(case((TradeRow.pnl > 0, TradeRow.pnl), else_=0)), 0),
        func.coalesce(func.sum(case((TradeRow.pnl < 0, -TradeRow.pnl), else_=0)), 0),
        func.avg(TradeRow.r_multiple),
    ).where(TradeRow.run_id == run_id)
    if since:
        total = total.where(TradeRow.closed_at >= since)
    _, wins, losses, even, pnl, gp, gl, expectancy = (await db.execute(total)).one()
    return {
        "strategy": by_strategy,
        "pair": by_pair,
        "month": by_month,
        "all": {
            "wins": int(wins),
            "losses": int(losses),
            "even": int(even),
            "pnl": Decimal(pnl),
            "gross_profit": Decimal(gp),
            "gross_loss": Decimal(gl),
            "expectancy_r": expectancy,
        },
    }


async def _avoided(
    db: AsyncSession, run_id: str, since: datetime | None, pause_hours: int | None = None
) -> list[AvoidedOut]:
    stmt = (
        select(DecisionRow.code, func.count())
        .where(
            DecisionRow.run_id == run_id, DecisionRow.traded.is_(False), DecisionRow.signal_id.is_not(None)
        )
        .group_by(DecisionRow.code)
        .order_by(func.count().desc())
    )
    if since:
        stmt = stmt.where(DecisionRow.decided_at >= since)
    return [
        AvoidedOut(code=code, label=translate(code, pair_pause_hours=pause_hours).text, count=int(count))
        for code, count in (await db.execute(stmt))
    ]


async def _position_intervals(db: AsyncSession, run_id: str) -> list[tuple[datetime, datetime | None]]:
    stmt = select(PositionRow.opened_at, PositionRow.closed_at).where(PositionRow.run_id == run_id)
    return [(a, b) for a, b in (await db.execute(stmt)).all()]


def _empty_performance(now: datetime) -> PerformanceOut:
    return PerformanceOut(
        run_id=None,
        mode="paper",
        simulated=True,
        period_start=None,
        period_end=now,
        trades=0,
        wins=0,
        losses=0,
        win_rate=None,
        profit_factor=None,
        payoff=None,
        expectancy_r=None,
        total_pnl=0.0,
        total_return_pct=None,
        max_drawdown_pct=None,
        sharpe=None,
        idle_pct=None,
        positive_months=0,
        months=0,
        by_strategy=[],
        by_pair=[],
        by_month=[],
        avoided=[],
        signals_refused=0,
        sample_warning="Ainda sem dados suficientes: os gráficos aparecem após a 1ª operação encerrada.",
        notes=["Ficar parado em USDT é uma decisão válida: não é falha do robô."],
    )


def _candle(row: CandleRow, timeframe: Timeframe) -> Candle:
    return Candle(
        symbol=row.symbol,
        timeframe=timeframe,
        open_time=row.open_time,
        open=row.open,
        high=row.high,
        low=row.low,
        close=row.close,
        volume=row.volume,
        closed=True,
    )


def _elephants(
    candles: Sequence[Candle], since: datetime | None, config: ElephantBarConfig
) -> list[MarkerOut]:
    found: list[MarkerOut] = []
    for index, candle in enumerate(candles):
        if since is not None and candle.open_time < since:
            continue
        result = detect_elephant_bar(candles, config, index)
        if result is not None and result.direction.value == "bullish":
            found.append(
                MarkerOut(
                    t=candle.open_time, kind="elephant", price=float(candle.high), text="Barra elefante"
                )
            )
    return found


def _trade_markers(rows: Sequence[TradeRow]) -> list[MarkerOut]:
    marks: list[MarkerOut] = []
    for r in rows:
        kind = {"stop_loss": "stop", "take_profit": "target"}.get(r.exit_reason, "exit")
        marks.append(
            MarkerOut(
                t=r.opened_at, kind="entry", price=float(r.entry_price), text="Entrada", run_id=r.run_id
            )
        )
        marks.append(
            MarkerOut(
                t=r.closed_at,
                kind=cast("Literal['exit', 'stop', 'target']", kind),
                price=float(r.exit_price),
                text=EXIT_REASONS.get(r.exit_reason, "Saída"),
                run_id=r.run_id,
            )
        )
    return marks


def _levels(p: PositionRow) -> list[LevelOut]:
    levels = [LevelOut(kind="stop", price=float(p.stop), since=p.opened_at)]
    levels.append(LevelOut(kind="entry", price=float(p.entry_price), since=p.opened_at))
    if p.take_profit is not None:
        levels.append(LevelOut(kind="target", price=float(p.take_profit), since=p.opened_at))
    return levels


def _day_stats(rows: Sequence[CandleRow], timeframe: Timeframe) -> tuple[float | None, float | None]:
    if not rows:
        return None, None
    per_day = max(1, 86_400 // timeframe.seconds)
    window = rows[-per_day:]
    first = window[0].open
    change = None if first <= 0 else round(float((rows[-1].close - first) / first * 100), 3)
    return change, float(sum((r.volume for r in window), Decimal(0)))
