import json
from datetime import datetime, timedelta
from decimal import Decimal

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from engine.application.ai.value import ApprovedTrade, VetoInput
from engine.application.backtest.settings import BacktestSettings
from engine.domain.candle import Candle
from engine.domain.signal import Signal
from engine.domain.types import Timeframe
from engine.infrastructure.persistence.models import (
    AiCallRow,
    CandleRow,
    DecisionRow,
    EquitySnapshotRow,
    FillRow,
    TradeRow,
)
from engine.infrastructure.persistence.serialization import require_utc

VETO_CODES = ("ai_veto", "ai_low_confidence")
MAX_ROWS = 500
_SIGNAL_FIELDS = frozenset(Signal.model_fields)


def _signal(stored: dict[str, object] | None) -> Signal | None:
    levels = {k: v for k, v in (stored or {}).items() if k in _SIGNAL_FIELDS}
    try:
        return Signal.model_validate_json(json.dumps(levels))
    except (ValidationError, ValueError):
        return None


class AiValueRepository:
    """Leitura para a medicao do valor da IA (nada e escrito)."""

    def __init__(self, session: AsyncSession, settings: BacktestSettings) -> None:
        self._session = session
        self._settings = settings

    async def vetoes(self, start: datetime, end: datetime) -> list[VetoInput]:
        stmt = (
            select(DecisionRow)
            .where(
                DecisionRow.code.in_(VETO_CODES),
                DecisionRow.decided_at >= require_utc(start, "start"),
                DecisionRow.decided_at <= require_utc(end, "end"),
            )
            .order_by(DecisionRow.decided_at.desc(), DecisionRow.id.desc())
            .limit(MAX_ROWS)
        )
        out: list[VetoInput] = []
        for row in await self._session.scalars(stmt):
            signal = _signal(row.signal)
            if signal is None:
                continue
            out.append(
                VetoInput(
                    decided_at=row.decided_at,
                    code=row.code,
                    strategy=signal.strategy,
                    symbol=row.symbol,
                    signal=signal,
                    equity=await self._equity(row.run_id, row.decided_at),
                    candles=await self._candles(signal),
                )
            )
        return out

    async def _equity(self, run_id: str, at: datetime) -> Decimal | None:
        stmt = (
            select(EquitySnapshotRow.equity)
            .where(EquitySnapshotRow.run_id == run_id, EquitySnapshotRow.at <= at)
            .order_by(EquitySnapshotRow.at.desc())
            .limit(1)
        )
        return await self._session.scalar(stmt)

    async def _candles(self, signal: Signal) -> tuple[Candle, ...]:
        step = timedelta(seconds=signal.timeframe.seconds)
        reach = self._settings.hypothetical_horizon + self._settings.risk.max_signal_age_timeframes + 3
        stmt = (
            select(CandleRow)
            .where(
                CandleRow.symbol == signal.symbol,
                CandleRow.timeframe == signal.timeframe.value,
                CandleRow.open_time >= signal.created_at - step,
                CandleRow.open_time < signal.created_at + step * reach,
            )
            .order_by(CandleRow.open_time)
        )
        return tuple(
            Candle(
                symbol=c.symbol,
                timeframe=Timeframe(c.timeframe),
                open_time=c.open_time,
                open=c.open,
                high=c.high,
                low=c.low,
                close=c.close,
                volume=c.volume,
                closed=True,
            )
            for c in await self._session.scalars(stmt)
        )

    async def approved(self, start: datetime, end: datetime) -> list[ApprovedTrade]:
        """Entradas aprovadas pela IA (decisao operada com `ai_detail`) e o que deram, perdas incluidas."""
        stmt = (
            select(DecisionRow)
            .where(
                DecisionRow.traded.is_(True),
                DecisionRow.signal.has_key("ai_detail"),
                DecisionRow.decided_at >= require_utc(start, "start"),
                DecisionRow.decided_at <= require_utc(end, "end"),
            )
            .order_by(DecisionRow.decided_at.desc(), DecisionRow.id.desc())
            .limit(MAX_ROWS)
        )
        out: list[ApprovedTrade] = []
        for row in await self._session.scalars(stmt):
            legs = await self._legs(row)
            if not legs:
                continue
            out.append(
                ApprovedTrade(
                    strategy=row.strategy or legs[0].strategy or "",
                    symbol=row.symbol,
                    pnl=float(sum(leg.pnl for leg in legs)),
                    r_multiple=float(sum(leg.r_multiple or 0 for leg in legs)),
                )
            )
        return out

    async def _legs(self, row: DecisionRow) -> list[TradeRow]:
        """decisao -> ordem (`client_order_id`) -> fill de compra -> trade da posicao aberta nesse fill.
        Ordem sem fill (expirada/cancelada) nao herda o trade de outra decisao: a posicao vai do fill desta
        ordem ate o proximo fill de compra do mesmo simbolo (so ha uma posicao aberta por simbolo e run)."""
        if row.client_order_id is None:
            return []
        filled_at = await self._session.scalar(
            select(func.min(FillRow.filled_at)).where(
                FillRow.run_id == row.run_id,
                FillRow.client_order_id == row.client_order_id,
                FillRow.side == "buy",
            )
        )
        if filled_at is None:
            return []
        next_entry = await self._session.scalar(
            select(func.min(FillRow.filled_at)).where(
                FillRow.run_id == row.run_id,
                FillRow.symbol == row.symbol,
                FillRow.side == "buy",
                FillRow.filled_at > filled_at,
                FillRow.client_order_id != row.client_order_id,
            )
        )
        window = [
            TradeRow.run_id == row.run_id,
            TradeRow.symbol == row.symbol,
            TradeRow.opened_at >= filled_at,
        ]
        if next_entry is not None:
            window.append(TradeRow.opened_at < next_entry)
        first = await self._session.scalar(select(func.min(TradeRow.opened_at)).where(*window))
        if first is None:
            return []
        return list(await self._session.scalars(select(TradeRow).where(*window, TradeRow.opened_at == first)))

    async def usage(self, start: datetime, end: datetime) -> tuple[int, int, float]:
        stmt = (
            select(AiCallRow.result, func.count(), func.coalesce(func.sum(AiCallRow.cost_usd), 0))
            .where(
                AiCallRow.called_at >= require_utc(start, "start"),
                AiCallRow.called_at <= require_utc(end, "end"),
                AiCallRow.result != "reserve",
            )
            .group_by(AiCallRow.result)
        )
        rows = (await self._session.execute(stmt)).all()
        calls = sum(int(r[1]) for r in rows)
        failed = sum(int(r[1]) for r in rows if r[0] in ("invalid", "timeout", "error"))
        return calls, failed, float(sum(Decimal(r[2]) for r in rows))
