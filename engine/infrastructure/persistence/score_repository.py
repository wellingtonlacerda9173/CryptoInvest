import uuid
from bisect import bisect_right
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import select, tuple_
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from engine.application.regime import Regime
from engine.application.selector import ScoreKey, StrategyScore, TradeSample, score_key
from engine.infrastructure.persistence.models import DecisionRow, RunRow, StrategyScoreRow, TradeRow
from engine.infrastructure.persistence.serialization import require_utc

Origin = Literal["backtest", "paper", "live"]


def _row(
    score: StrategyScore, snapshot_id: str, timeframe: str, origin: Origin, run_id: str | None, at: datetime
) -> StrategyScoreRow:
    return StrategyScoreRow(
        snapshot_id=snapshot_id,
        strategy=score.strategy,
        regime=score.regime.value,
        symbol=score.symbol,
        timeframe=timeframe,
        sample_size=score.trades,
        score=score.score,
        eligible=score.eligible,
        ineligible_reason=score.ineligible_reason,
        detail=score.model_dump(mode="json"),
        calculated_at=at,
        origin=origin,
        run_id=run_id,
    )


class ScoreRepository:
    """Snapshots append-only de scores do seletor: so `publish` e leituras (o banco recusa UPDATE/DELETE)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def publish(
        self,
        scores: Sequence[StrategyScore],
        *,
        timeframe: str,
        origin: Origin,
        calculated_at: datetime,
        run_id: str | None = None,
        snapshot_id: str | None = None,
    ) -> str:
        at = require_utc(calculated_at, "calculated_at")
        snapshot = snapshot_id or uuid.uuid4().hex
        self._session.add_all(_row(s, snapshot, timeframe, origin, run_id, at) for s in scores)
        await self._session.flush()
        return snapshot

    async def latest(
        self, timeframe: str, symbols: Sequence[str] | None = None
    ) -> dict[ScoreKey, StrategyScore]:
        """Para cada par, os scores do snapshot mais recente que o contem."""
        newest = (
            select(StrategyScoreRow.symbol, StrategyScoreRow.snapshot_id)
            .where(StrategyScoreRow.timeframe == timeframe)
            .ext(distinct_on(StrategyScoreRow.symbol))
            .order_by(
                StrategyScoreRow.symbol, StrategyScoreRow.calculated_at.desc(), StrategyScoreRow.id.desc()
            )
        )
        if symbols is not None:
            newest = newest.where(StrategyScoreRow.symbol.in_(symbols))
        pairs = [(r.symbol, r.snapshot_id) for r in (await self._session.execute(newest)).all()]
        if not pairs:
            return {}
        stmt = select(StrategyScoreRow).where(
            StrategyScoreRow.timeframe == timeframe,
            tuple_(StrategyScoreRow.symbol, StrategyScoreRow.snapshot_id).in_(pairs),
        )
        out: dict[ScoreKey, StrategyScore] = {}
        for row in await self._session.scalars(stmt):
            score = StrategyScore.model_validate(row.detail)
            out[score_key(score.strategy, score.regime, score.symbol)] = score
        return out

    async def journal_samples(self, timeframe_symbols: Sequence[str] | None = None) -> list[TradeSample]:
        """Round-trips do diario (paper/live) rotulados com o regime registrado na decisao de entrada.

        Trades de runs sem seletor nao trazem regime e ficam de fora (nao ha como rotula-los sem inventar).
        """
        decisions: dict[tuple[str, str], list[tuple[datetime, str, str | None]]] = defaultdict(list)
        stmt_d = select(
            DecisionRow.run_id,
            DecisionRow.symbol,
            DecisionRow.decided_at,
            DecisionRow.signal,
            DecisionRow.strategy,
        ).where(DecisionRow.traded.is_(True), DecisionRow.signal.is_not(None))
        for d in (await self._session.execute(stmt_d)).all():
            code = (d.signal or {}).get("regime_code")
            if isinstance(code, str):
                decisions[(d.run_id, d.symbol)].append((d.decided_at, code, d.strategy))
        for items in decisions.values():
            items.sort(key=lambda x: x[0])
        stmt = (
            select(TradeRow, RunRow.mode)
            .join(RunRow, RunRow.run_id == TradeRow.run_id)
            .where(RunRow.mode.in_(("paper", "live")), TradeRow.r_multiple.is_not(None))
            .order_by(TradeRow.closed_at, TradeRow.id)
        )
        groups: dict[tuple[str, str, datetime], list[Any]] = defaultdict(list)
        modes: dict[str, str] = {}
        for trade, mode in (await self._session.execute(stmt)).all():
            if timeframe_symbols is not None and trade.symbol not in timeframe_symbols:
                continue
            groups[(trade.run_id, trade.symbol, trade.opened_at)].append(trade)
            modes[trade.run_id] = mode
        samples: list[TradeSample] = []
        for (run_id, symbol, opened_at), legs in groups.items():
            known = decisions.get((run_id, symbol), [])
            index = bisect_right([d[0] for d in known], opened_at) - 1
            strategy = next((leg.strategy for leg in legs if leg.strategy), None)
            if index < 0 or strategy is None or known[index][2] not in (None, strategy):
                continue
            samples.append(
                TradeSample(
                    strategy=strategy,
                    symbol=symbol,
                    regime=Regime(known[index][1]),
                    operable=True,
                    opened_at=opened_at,
                    closed_at=max(leg.closed_at for leg in legs),
                    r_multiple=float(sum(leg.r_multiple for leg in legs)),
                    source=modes[run_id],
                )
            )
        return sorted(samples, key=lambda s: s.closed_at)
