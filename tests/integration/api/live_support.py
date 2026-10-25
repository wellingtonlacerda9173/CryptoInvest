"""Semeadura de runs de Paper para a trava do Live: um run que cumpre TODOS os criterios e variantes que
violam exatamente um deles. Tudo em banco descartavel (`*_test`)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from engine.infrastructure.persistence.models import (
    AuditLogRow,
    ControlEventRow,
    EquitySnapshotRow,
    RunRow,
    TradeRow,
)

D = Decimal
GOOD = "paper-live-ok"


async def seed_paper_run(
    sessions: async_sessionmaker[AsyncSession],
    run_id: str,
    *,
    days: int = 70,
    trades: int = 40,
    losses: int = 8,
    win: str = "100",
    loss: str = "-40",
    big_loss: str | None = None,
    open_calendar: bool = False,
    risk: dict[str, str] | None = None,
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    start = now - timedelta(days=days)
    loss_idx = sorted(range(2, trades, max(1, trades // losses)))[:losses]
    pnls = [
        (start + timedelta(days=days * (index + 0.5) / trades), D(loss if index in loss_idx else win))
        for index in range(trades)
    ]
    if big_loss is not None:
        first_loss = next(i for i, (_, p) in enumerate(pnls) if p < 0)
        pnls[first_loss] = (pnls[first_loss][0], D(big_loss))
    config = {
        "config": {
            "config": {
                "capital": "10000",
                "timeframe": "1h",
                "symbols": ["BTC/USDT"],
                "allow_open_calendar": open_calendar,
                **({"risk": risk} if risk else {}),
            },
            "config_hash": "x",
        },
        "code_version": "abc123",
        "reason": "inicio do run",
    }
    async with sessions.begin() as db:
        db.add(
            RunRow(run_id=run_id, mode="paper", started_at=start, config_hash="h" * 32, code_version="abc")
        )
        await db.flush()
        db.add(AuditLogRow(actor="system", action="paper_start", entity=run_id, payload=config))
        db.add(ControlEventRow(run_id=run_id, at=start, action="start", operator="system", reason="inicio"))
        for index, (closed, pnl) in enumerate(pnls):
            db.add(
                TradeRow(
                    trade_id=f"{run_id}-t{index}",
                    run_id=run_id,
                    symbol="BTC/USDT",
                    quantity=D(1),
                    entry_price=D(1000),
                    exit_price=D(1000) + pnl + D("0.2"),
                    entry_fee=D("0.1"),
                    exit_fee=D("0.1"),
                    pnl=pnl,
                    opened_at=closed - timedelta(hours=6),
                    closed_at=closed,
                    exit_reason="take_profit" if pnl > 0 else "stop_loss",
                    strategy="elephant",
                    risk_amount=D("50"),
                    r_multiple=D("2") if pnl > 0 else D("-1"),
                )
            )
        realized = D(10_000)
        pending = list(pnls)
        for hour in range(days * 24 + 1):
            at = start + timedelta(hours=hour)
            while pending and pending[0][0] <= at:
                realized += pending.pop(0)[1]
            db.add(
                EquitySnapshotRow(run_id=run_id, at=at, equity=realized, realized=realized, in_position=False)
            )
