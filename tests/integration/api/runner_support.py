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
from engine.runner import demo_seed
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
    return await demo_seed.seed_run(sessions, run_id, open_run_id=OPEN_RUN_ID, candles=True)


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
