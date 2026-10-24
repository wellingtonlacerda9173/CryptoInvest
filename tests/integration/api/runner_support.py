"""Apoio dos testes de integracao da API de trading/controle: banco por modulo, link fake ou Redis real."""

import asyncio
import secrets
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import URL
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.app import create_app
from api.binance_keys import FakeKeyVerifier
from api.config import ApiSettings
from api.runner_link import RedisRunnerLink
from engine.application.risk import RiskState
from engine.domain.risk import RiskConfig
from engine.infrastructure.config.settings import Settings
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
from engine.runner.paper_events import RedisKeys
from tests.api_support import FakeClock, make_settings
from tests.integration.api.conftest import Harness, Session
from tests.integration.disposable_db import assert_disposable_database, disposable_database, upgrade_database
from tests.redis_fakes import FakeLink, FakeSource

COMMAND_KEY = "k" * 48
RUN_ID = "paper-it-1"
OPEN_RUN_ID = "paper-it-open"
D = Decimal


def redis_url_of(settings: Settings) -> str:
    """`localhost` resolve primeiro para ::1 em algumas maquinas (Docker so publica em 127.0.0.1): evita o atraso."""
    return settings.redis_url.replace("//localhost", "//127.0.0.1")


def runner_settings(**overrides: Any) -> ApiSettings:
    return make_settings(command_hmac_key=SecretStr(COMMAND_KEY), **overrides)


@pytest.fixture(scope="module")
def module_db_url(settings: Settings, db_ready: None) -> Iterator[URL]:
    """Banco descartavel so deste modulo (trades/decisoes sao append-only: nao da para limpar entre testes)."""
    with disposable_database(settings, "reads") as url:
        upgrade_database(url)
        yield url


@pytest_asyncio.fixture
async def module_sessions(module_db_url: URL) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    assert_disposable_database(module_db_url)
    engine = create_engine(module_db_url)
    yield create_session_factory(engine)
    await engine.dispose()


def build_harness(
    sessions: async_sessionmaker[AsyncSession], link: Any, source: Any = None, **settings: Any
) -> Harness:
    clock = FakeClock()
    verifier = FakeKeyVerifier()
    app = create_app(
        runner_settings(**settings),
        sessions=sessions,
        verifier=verifier,
        clock=clock,
        link=link,
        events=source or FakeSource(),
        background=False,
    )
    return Harness(app=app, container=app.state.container, clock=clock, verifier=verifier, sessions=sessions)


@pytest.fixture
def fake_link() -> FakeLink:
    return FakeLink()


@pytest_asyncio.fixture
async def rh(module_sessions: async_sessionmaker[AsyncSession], fake_link: FakeLink) -> Harness:
    return build_harness(module_sessions, fake_link, fake_link.source)


@pytest_asyncio.fixture
async def rh_admin(rh: Harness) -> AsyncIterator[Session]:
    session = await rh.login(await rh.create_user("admin"))
    yield session
    await session.aclose()


@pytest_asyncio.fixture
async def rh_reader(rh: Harness) -> AsyncIterator[Session]:
    session = await rh.login(await rh.create_user("reader"))
    yield session
    await session.aclose()


@pytest_asyncio.fixture
async def redis_link(settings: Settings) -> AsyncIterator[RedisRunnerLink]:
    """Redis real com prefixo exclusivo (canais e chaves do teste nunca tocam os do robo de desenvolvimento)."""
    link = RedisRunnerLink(redis_url_of(settings), RedisKeys(f"cryptoinvest:test:{secrets.token_hex(4)}"))
    if not (await link.probe()).reachable:
        await link.close()
        pytest.skip("Redis indisponivel (docker compose -f docker-compose.dev.yml up -d)")
    yield link
    await link.close()


def status_payload(now: datetime | None = None, **overrides: Any) -> dict[str, Any]:
    """O que o runner publica no heartbeat (campos de `PaperRobot.status_payload`)."""
    now = now or datetime.now(UTC)
    base: dict[str, Any] = {
        "kind": "heartbeat",
        "run_id": RUN_ID,
        "at": now.isoformat(),
        "state": "RUNNING",
        "heartbeat_at": now.isoformat(),
        "started_at": (now - timedelta(hours=2)).isoformat(),
        "code_version": "abc123",
        "stops_blind": False,
        "halted": False,
        "halt_reason": None,
        "paused": False,
        "pause_reason": None,
        "pause_reasons": [],
        "equity": "10184.32",
        "realized_equity": "10200.00",
        "peak_equity": "10300.00",
        "drawdown": "0.0113",
        "day": now.date().isoformat(),
        "day_pnl": "-30.56",
        "day_start_equity": "10214.88",
        "daily_pause_until": None,
        "target_reached_on": None,
        "pair_pause_until": {},
        "loss_streaks": {"ETH/USDT": 1},
        "exposure": "0.24",
        "coin_exposure": {"BTC": "0.24"},
        "open_positions": ["BTC/USDT"],
        "pending_entries": [],
        "redis_healthy": True,
        "allow_open_calendar": False,
        "config_hash": "c" * 32,
        "config_version": None,
        "applied_config_version": None,
        "risk": RiskConfig().model_dump(mode="json"),
        "outbox_pending": 0,
        "persistence_error": None,
    }
    return base | overrides


# --------------------------------------------------------------------------- dados de exemplo


async def seed_run(sessions: async_sessionmaker[AsyncSession], run_id: str = RUN_ID) -> dict[str, Any]:
    """Run de paper com 10 dias de historico: equity, 8 operacoes, decisoes, posicao aberta e velas."""
    now = datetime.now(UTC).replace(microsecond=0)
    start = now - timedelta(days=10)
    config = {
        "config": {
            "config": {"capital": "10000", "timeframe": "1h", "symbols": ["BTC/USDT", "ETH/USDT"], "allow_open_calendar": False},
            "config_hash": "x",
        },
        "code_version": "abc123",
        "reason": "inicio do run",
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
            RunRow(run_id=run_id, mode="paper", started_at=start, config_hash="h" * 32, code_version="abc123")
        )
        await db.flush()
        db.add(AuditLogRow(actor="system", action="paper_start", entity=run_id, payload=config))
        db.add(
            ControlEventRow(
                run_id=run_id, at=start, action="start", operator="system", reason="inicio do run"
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
                    trade_id=f"t{index}", run_id=run_id, symbol=symbol, quantity=D(1), entry_price=D(1000),
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
        for index, (code, traded, action, strategy, symbol, has_signal) in enumerate(decisions):
            signal = (
                {
                    "symbol": symbol,
                    "entry": "100",
                    "stop_loss": "95",
                    "take_profit": "110",
                    "strategy": strategy,
                }
                if has_signal
                else None
            )
            db.add(
                DecisionRow(
                    decision_key=f"d{index}", run_id=run_id, decided_at=now - timedelta(seconds=index),
                    symbol=symbol, action=action, traded=traded, code=code, reason=f"{code}: mensagem tecnica",
                    strategy=strategy, confidence=0.7 if has_signal else None,
                    signal_id=f"s{index}" if has_signal else None, signal=signal, context={},
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
    async with sessions.begin() as db:
        older = now - timedelta(days=30)
        db.add(
            RunRow(
                run_id=OPEN_RUN_ID, mode="paper", started_at=older, config_hash="o" * 32, code_version="abc"
            )
        )
        await db.flush()
        open_config = {
            "config": {"config": {"capital": "5000", "timeframe": "1h", "allow_open_calendar": True}}
        }
        db.add(AuditLogRow(actor="system", action="paper_start", entity=OPEN_RUN_ID, payload=open_config))
        db.add(
            ControlEventRow(run_id=OPEN_RUN_ID, at=older, action="start", operator="system", reason="inicio")
        )
    return {"now": now, "start": start, "trades": len(trades)}


def seed_once(url: URL) -> Callable[[], dict[str, Any]]:
    def run() -> dict[str, Any]:
        engine = create_engine(url)

        async def go() -> dict[str, Any]:
            try:
                return await seed_run(create_session_factory(engine))
            finally:
                await engine.dispose()

        return asyncio.run(go())

    return run
