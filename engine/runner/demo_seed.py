"""Dados FICTICIOS de exemplo: alimentam testes de integracao/E2E e a demonstracao local (`deploy/local-up demo`).

`python -m engine.runner.demo_seed` so roda contra o banco `cryptoinvest_demo` (separado do diario real): grava um run
`demo-paper` rotulado ("DEMO"), com calendario aberto (inelegivel para o Live) e sem velas. O diario real nunca e tocado:
o banco demo e descartado inteiro (`DROP DATABASE`), pois trades/decisoes sao append-only.
"""

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from engine.application.risk import RiskState
from engine.infrastructure.config.settings import load_settings
from engine.infrastructure.persistence.models import (
    AuditLogRow,
    CandleRow,
    ControlEventRow,
    DecisionRow,
    EquitySnapshotRow,
    PositionRow,
    RunRow,
    TradeRow,
)
from engine.infrastructure.persistence.session import create_engine, create_session_factory
from engine.infrastructure.persistence.state_repository import RiskStateRepository

D = Decimal
DEMO_DATABASE = "cryptoinvest_demo"
DEMO_RUN_ID = "demo-paper"
DEMO_LABEL = "DEMO · "
SCREEN_TRADES = (
    (20, "115.5", "113", "stop_loss", "-25.2", "-1.0"),
    (8, "118", "121", "take_profit", "29.8", "1.2"),
)


async def seed_run(
    sessions: async_sessionmaker[AsyncSession],
    run_id: str,
    *,
    open_run_id: str | None = None,
    candles: bool = False,
    label: str = "",
    code_version: str = "abc123",
    allow_open_calendar: bool = False,
    id_prefix: str = "",
) -> dict[str, Any]:
    """Run de paper com 10 dias de historico: equity, 9 operacoes, decisoes e posicao aberta. `candles` grava velas
    BTC/USDT sinteticas (so em banco de teste: sao dados de mercado compartilhados); `open_run_id` cria um 2o run
    mais antigo com calendario aberto; `label` prefixa os motivos (ex.: "DEMO · ")."""
    now = datetime.now(UTC).replace(microsecond=0)
    start = now - timedelta(days=10)
    config = {
        "config": {
            "config": {"capital": "10000", "timeframe": "1h", "symbols": ["BTC/USDT", "ETH/USDT"], "allow_open_calendar": allow_open_calendar},
            "config_hash": "x",
        },
        "code_version": code_version,
        "reason": f"{label}inicio do run",
    }  # fmt: skip
    trades = [
        # (dias atras, simbolo, estrategia, motivo, pnl, r)
        (9, "BTC/USDT", "elephant", "take_profit", "120.50", "2.0"),
        (8, "ETH/USDT", "ema", "stop_loss", "-50.00", "-1.0"),
        (7, "BTC/USDT", "elephant", "stop_loss", "-48.00", "-1.0"),
        (5, "BTC/USDT", "elephant", "take_profit", "98.00", "2.0"),
        (4, "ETH/USDT", "ema", "profit_protection", "30.00", "0.6"),
        (3, "BTC/USDT", "ema", "stop_loss", "-52.00", "-1.0"),
        (2, "ETH/USDT", "elephant", "signal", "0.00", "0.0"),
        (1, "BTC/USDT", "elephant", "take_profit", "101.00", "2.0"),
        (0.5, "ETH/USDT", "=1+1", "take_profit", "10.00", "0.2"),
    ]
    async with sessions.begin() as db:
        db.add(
            RunRow(
                run_id=run_id, mode="paper", started_at=start, config_hash="h" * 32, code_version=code_version
            )
        )
        await db.flush()
        db.add(AuditLogRow(actor="system", action="paper_start", entity=run_id, payload=config))
        db.add(
            ControlEventRow(
                run_id=run_id, at=start, action="start", operator="system", reason=f"{label}inicio do run"
            )
        )
        equity = D(10_000)
        for hour in range(0, 10 * 24, 1):
            at = start + timedelta(hours=hour)
            equity += D("1.2") if hour % 5 else D("-0.4")
            db.add(
                EquitySnapshotRow(
                    run_id=run_id, at=at, equity=equity, realized=equity, in_position=hour % 7 == 0
                )
            )
        for index, (days, symbol, strategy, reason, pnl, r) in enumerate(trades):
            closed = now - timedelta(days=days)
            db.add(
                TradeRow(
                    trade_id=f"{id_prefix}t{index}", run_id=run_id, symbol=symbol, quantity=D(1), entry_price=D(1000),
                    exit_price=D(1000) + D(pnl) + D("0.2"), entry_fee=D("0.1"), exit_fee=D("0.1"), pnl=D(pnl),
                    opened_at=closed - timedelta(hours=6), closed_at=closed, exit_reason=reason, strategy=strategy,
                    risk_amount=D("50"), r_multiple=D(r),
                )
            )  # fmt: skip
        decisions = [
            ("approved", True, "enter", "elephant", "BTC/USDT", True),
            ("no_signal", False, "skip", None, "BTC/USDT", False),
            ("no_signal", False, "skip", None, "ETH/USDT", False),
            ("event_window", False, "skip", "ema", "ETH/USDT", True),
            ("reward_risk", False, "skip", "elephant", "BTC/USDT", True),
            ("spread_too_wide", False, "skip", "ema", "BTC/USDT", True),
            ("catch_up", False, "skip", None, "BTC/USDT", False),
        ]
        for index, (code, traded, action, strat, symbol, has_signal) in enumerate(decisions):
            signal = (
                {
                    "symbol": symbol,
                    "entry": "100",
                    "stop_loss": "95",
                    "take_profit": "110",
                    "strategy": strat,
                }
                if has_signal
                else None
            )
            db.add(
                DecisionRow(
                    decision_key=f"{id_prefix}d{index}", run_id=run_id, decided_at=now - timedelta(seconds=index),
                    symbol=symbol, action=action, traded=traded, code=code, reason=f"{label}{code}: mensagem tecnica",
                    strategy=strat, confidence=0.7 if has_signal else None,
                    signal_id=f"{id_prefix}s{index}" if has_signal else None, signal=signal, context={},
                )
            )  # fmt: skip
        db.add(
            PositionRow(
                run_id=run_id, symbol="BTC/USDT", entry_price=D("100"), initial_quantity=D("1"), quantity=D("1"),
                initial_stop=D("95"), stop=D("97"), take_profit=D("110"), highest_price=D("104"),
                entry_fee=D("0.1"), breakeven_reached=False, partial_done=False, realized_pnl=D(0),
                opened_at=now - timedelta(hours=3),
            )
        )  # fmt: skip
        await db.flush()
        await RiskStateRepository(db).save(
            run_id,
            RiskState.initial(D("10214.88"), start).model_copy(
                update={"equity": D("10184.32"), "peak_equity": D("10300"), "day": now.date(), "day_pnl": D("-30.56"),
                        "day_start_equity": D("10214.88")}
            ),
            saved_at=now,
        )  # fmt: skip
        if candles:
            base = now.replace(minute=0, second=0) - timedelta(hours=340)
            price = D(100)
            for i in range(340):
                big = i == 330
                open_ = price
                close = price + (D("6") if big else D("0.05"))
                db.add(
                    CandleRow(
                        symbol="BTC/USDT", timeframe="1h", open_time=base + timedelta(hours=i), open=open_,
                        high=max(open_, close) + D("0.1"), low=min(open_, close) - D("0.1"), close=close,
                        volume=D("5000") if big else D("1000"),
                    )
                )  # fmt: skip
                price = close
    if open_run_id is not None:
        async with sessions.begin() as db:
            older = now - timedelta(days=30)
            db.add(
                RunRow(
                    run_id=open_run_id,
                    mode="paper",
                    started_at=older,
                    config_hash="o" * 32,
                    code_version="abc",
                )
            )
            await db.flush()
            open_config = {
                "config": {"config": {"capital": "5000", "timeframe": "1h", "allow_open_calendar": True}}
            }
            db.add(AuditLogRow(actor="system", action="paper_start", entity=open_run_id, payload=open_config))
            db.add(
                ControlEventRow(
                    run_id=open_run_id, at=older, action="start", operator="system", reason="inicio"
                )
            )
    return {"now": now, "start": start, "trades": len(trades)}


async def seed_screens(
    sessions: async_sessionmaker[AsyncSession], run_id: str, *, label: str = "", id_prefix: str = "e2e"
) -> None:
    """Dados extras das telas Mercado/Decisoes/Operacoes: uma decisao com regime, notas e parecer de IA, e uma
    perda e um ganho em precos coerentes com as velas."""
    now = datetime.now(UTC).replace(microsecond=0)
    async with sessions.begin() as db:
        db.add(
            DecisionRow(
                decision_key=f"{id_prefix}-rich", run_id=run_id, decided_at=now - timedelta(seconds=30),
                symbol="BTC/USDT", action="skip", traded=False, code="reward_risk",
                reason=f"{label}reward_risk: tecnico", strategy="elephant", confidence=0.72,
                signal_id=f"{id_prefix}-sig",
                signal={
                    "symbol": "BTC/USDT", "strategy": "elephant", "entry": "123", "stop_loss": "120",
                    "take_profit": "127", "regime": "tendência de alta",
                    "scores": [
                        {"strategy": "elephant", "score": 0.72},
                        {"strategy": "ema", "score": 0.41},
                    ],
                    "ai_opinion": "Contexto favorável, mas o ganho é pequeno para o risco.",
                },
                context={},
            )
        )  # fmt: skip
        for index, (hours, entry, exit_, reason, pnl, r) in enumerate(SCREEN_TRADES):
            closed = now - timedelta(hours=hours)
            db.add(
                TradeRow(
                    trade_id=f"{id_prefix}{index}", run_id=run_id, symbol="BTC/USDT", quantity=D(10),
                    entry_price=D(entry), exit_price=D(exit_), entry_fee=D("0.1"),
                    exit_fee=D("0.1"), pnl=D(pnl), opened_at=closed - timedelta(hours=5),
                    closed_at=closed, exit_reason=reason, strategy="elephant",
                    risk_amount=D("25"), r_multiple=D(r),
                )
            )  # fmt: skip


async def seed_demo(sessions: async_sessionmaker[AsyncSession]) -> dict[str, Any]:
    result = await seed_run(
        sessions,
        DEMO_RUN_ID,
        label=DEMO_LABEL,
        code_version="demo",
        allow_open_calendar=True,
        id_prefix="demo-",
    )
    await seed_screens(sessions, DEMO_RUN_ID, label=DEMO_LABEL, id_prefix="demo-s")
    return result


async def _run() -> int:
    settings = load_settings()
    if settings.database_name != DEMO_DATABASE:
        print(
            f"recusado: a demonstracao so grava no banco {DEMO_DATABASE}, nunca em {settings.database_name}"
        )
        return 2
    engine = create_engine(settings.database_url)
    try:
        sessions = create_session_factory(engine)
        async with sessions() as db:
            foreign = list(
                (await db.scalars(select(RunRow.run_id).where(~RunRow.run_id.like("demo-%")))).all()
            )
            existing = await db.get(RunRow, DEMO_RUN_ID)
        if foreign or existing is not None:
            print("recusado: o banco de demonstracao ja tem dados (rode `demo --remove` e depois `demo`)")
            return 2
        result = await seed_demo(sessions)
        print(
            f"demo: run {DEMO_RUN_ID} criado com {result['trades'] + len(SCREEN_TRADES)} operacoes (dados ficticios)"
        )
        return 0
    finally:
        await engine.dispose()


def main() -> int:
    return asyncio.run(_run())


if __name__ == "__main__":
    sys.exit(main())
