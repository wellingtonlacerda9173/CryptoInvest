"""Coleta do diario (Timescale) e do backtest comparavel para o relatorio do paper."""

from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from engine.application.backtest import BacktestEngine, BacktestSettings, default_rules
from engine.application.paper_metrics import active_intervals
from engine.infrastructure.persistence.models import AuditLogRow, DecisionRow
from engine.infrastructure.persistence.run_repository import (
    ControlEventRepository,
    EquitySnapshotRepository,
    RunRepository,
)
from engine.infrastructure.persistence.trade_repository import TradeRepository
from engine.runner.backtest import build_strategies
from engine.runner.candle_source import floor_to_timeframe, load_candles
from engine.runner.paper_config import PaperRunConfig
from engine.runner.paper_report import BacktestSide, DecisionSummary, ReportInputs


class UnknownRunError(LookupError):
    """O run pedido nao existe no diario."""


async def _stored_config(session: AsyncSession, run_id: str) -> PaperRunConfig:
    stmt = (
        select(AuditLogRow.payload)
        .where(AuditLogRow.entity == run_id, AuditLogRow.action == "paper_start")
        .order_by(AuditLogRow.id.desc())
        .limit(1)
    )
    payload = await session.scalar(stmt)
    if payload is None:
        raise UnknownRunError(f"configuracao do run {run_id} nao encontrada no audit_log")
    return PaperRunConfig.model_validate(payload["config"]["config"])


async def used_open_calendar(session: AsyncSession, run_id: str) -> bool:
    """O run operou SEM calendario em algum momento? Vale qualquer `paper_start` do audit_log (inclusive os
    de reinicio) ou qualquer decisao marcada `calendar_open`: reiniciar sem a flag nao lava o historico."""
    starts = await session.scalars(
        select(AuditLogRow.payload).where(AuditLogRow.entity == run_id, AuditLogRow.action == "paper_start")
    )
    for payload in starts:
        config = (payload.get("config") or {}).get("config") or {}
        if config.get("allow_open_calendar") is True:
            return True
    flagged = await session.scalar(
        select(DecisionRow.id)
        .where(DecisionRow.run_id == run_id, DecisionRow.context["calendar_open"].as_boolean().is_(True))
        .limit(1)
    )
    return flagged is not None


async def collect_inputs(
    sessions: async_sessionmaker[AsyncSession],
    run_id: str,
    *,
    now: datetime,
    compare: bool = True,
    cache_dir: Path = Path("data/cache"),
    use_db_candles: bool = True,
) -> ReportInputs:
    async with sessions() as session:
        run = await RunRepository(session).get(run_id)
        if run is None:
            raise UnknownRunError(f"run {run_id} nao existe")
        config = await _stored_config(session, run_id)
        open_calendar = config.allow_open_calendar or await used_open_calendar(session, run_id)
        trades = await TradeRepository(session).list_for_run(run_id)
        curve = await EquitySnapshotRepository(session).list_for_run(run_id)
        events = await ControlEventRepository(session).list_for_run(run_id)
        rows = await session.execute(
            select(DecisionRow.decided_at, DecisionRow.symbol, DecisionRow.code, DecisionRow.traded)
            .where(DecisionRow.run_id == run_id)
            .order_by(DecisionRow.decided_at, DecisionRow.id)
        )
        decisions = [DecisionSummary(at, symbol, code, traded) for at, symbol, code, traded in rows]
    inputs = ReportInputs(
        run_id=run_id,
        capital=config.capital,
        risk=config.risk,
        timeframe=config.timeframe,
        symbols=config.symbols,
        strategies=config.strategies,
        trades=trades,
        curve=curve,
        events=events,
        decisions=decisions,
        now=now,
        allow_open_calendar=open_calendar,
        code_version=run.code_version,
    )
    if compare:
        inputs.backtests = [
            await backtest_side(config, inputs, symbol, cache_dir, use_db_candles)
            for symbol in config.symbols
        ]
    return inputs


async def backtest_side(
    config: PaperRunConfig, inputs: ReportInputs, symbol: str, cache_dir: Path, use_db: bool
) -> BacktestSide:
    """Roda o backtest nos MESMOS candles do periodo ativo do paper (com aquecimento dos indicadores)."""
    intervals = active_intervals(inputs.events, inputs.now)
    if not intervals:
        return BacktestSide(symbol, note="run sem periodo ativo")
    step = timedelta(seconds=config.timeframe.seconds)
    first = floor_to_timeframe(intervals[0][0], config.timeframe)
    period_start = first if first >= intervals[0][0] else first + step
    period_end = floor_to_timeframe(inputs.now, config.timeframe)
    if period_end <= period_start:
        return BacktestSide(symbol, note="periodo menor que uma vela fechada do timeframe")
    loaded = await load_candles(
        symbol,
        config.timeframe,
        period_start - step * config.history_window,
        period_end,
        cache_dir=cache_dir,
        use_db=use_db,
    )
    candles = loaded.candles
    start = next((i for i, c in enumerate(candles) if c.open_time >= period_start), None)
    if start is None or len(candles) - start < 2:
        return BacktestSide(symbol, candles=len(candles), note="candles insuficientes no periodo")
    settings = BacktestSettings(
        initial_capital=config.capital,
        maker_fee_rate=config.maker_fee,
        slippage_bps=config.slippage_bps,
        risk=config.risk,
        history_window=config.history_window,
    )
    result = await BacktestEngine(
        build_strategies(config.strategies, config.params),
        settings,
        rules=default_rules(symbol),
        clock=lambda: inputs.now,
    ).run(candles, start=start)
    return BacktestSide(symbol, trades=result.trades, candles=len(candles) - start)
