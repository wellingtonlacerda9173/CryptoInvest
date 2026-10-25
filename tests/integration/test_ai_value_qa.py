"""Evidencia de QA da Fase 6: associacao decisao aprovada pela IA -> trade, em banco descartavel `*_test`."""

import socket
from collections.abc import AsyncIterator, Iterator
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import URL
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from engine.application.ai.value import ApprovedTrade
from engine.application.backtest.settings import BacktestSettings
from engine.domain.candle import Candle
from engine.domain.journal import DecisionEntry
from engine.domain.order import OrderStatus
from engine.domain.signal import Signal
from engine.domain.trade import Trade
from engine.domain.types import Timeframe
from engine.infrastructure.config.settings import Settings, load_settings
from engine.infrastructure.persistence.ai_value_repository import AiValueRepository
from engine.infrastructure.persistence.candle_repository import CandleRepository
from engine.infrastructure.persistence.decision_repository import DecisionRepository
from engine.infrastructure.persistence.order_repository import FillRepository, OrderRepository
from engine.infrastructure.persistence.run_repository import RunRepository
from engine.infrastructure.persistence.session import create_engine, create_session_factory
from engine.infrastructure.persistence.trade_repository import TradeRepository
from tests.integration.disposable_db import assert_disposable_database, disposable_database
from tests.integration.journal_helpers import SYMBOL, T0, decision, fill, new_run, order, trade

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def db_settings() -> Settings:
    try:
        loaded = load_settings()
    except Exception as exc:
        pytest.skip(f"settings indisponiveis: {exc.__class__.__name__}")
    try:
        with socket.create_connection((loaded.database_host, loaded.database_port), timeout=3):
            pass
    except OSError:
        pytest.skip("Postgres/Timescale indisponivel")
    return loaded


@pytest.fixture
def head_url(db_settings: Settings) -> Iterator[URL]:
    with disposable_database(db_settings, "aivalqa") as url:
        assert_disposable_database(url)
        config = Config("alembic.ini")
        config.attributes["database_url"] = url
        command.upgrade(config, "head")
        yield url


@pytest_asyncio.fixture
async def sessions(head_url: URL) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine: AsyncEngine = create_engine(head_url)
    yield create_session_factory(engine)
    await engine.dispose()


AI = {"ai_detail": {"verdict": "approve", "confidence": 0.9, "reason": "ok", "untrusted": True}}


def _approved(at: datetime, order_id: str, signal_id: str) -> DecisionEntry:
    return decision(at=at).model_copy(
        update={"insight": AI, "signal_id": signal_id, "client_order_id": order_id}
    )


def _leg(trade_id: str, opened: datetime, exit_price: str, hours: int) -> Trade:
    return trade(trade_id, exit_price=exit_price).model_copy(
        update={"opened_at": opened, "closed_at": opened + timedelta(hours=hours)}
    )


async def _window(sessions: async_sessionmaker[AsyncSession]) -> list[ApprovedTrade]:
    async with sessions() as db:
        return await AiValueRepository(db, BacktestSettings()).approved(
            T0 - timedelta(days=1), T0 + timedelta(days=1)
        )


async def test_decisao_aprovada_sem_fill_nao_herda_o_trade_de_outra_decisao(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    run = new_run()
    later = T0 + timedelta(hours=2)
    filled = _leg("t-1", later + timedelta(seconds=5), "90", 3)
    async with sessions() as db, db.begin():
        await RunRepository(db).start(run)
        repo = DecisionRepository(db)
        await repo.record(run.run_id, _approved(T0, "o-a", "a"))
        await repo.record(run.run_id, _approved(later, "o-b", "b"))
        orders = OrderRepository(db)
        await orders.upsert(run.run_id, order("o-a", status=OrderStatus.EXPIRED))
        await orders.upsert(run.run_id, order("o-b", status=OrderStatus.FILLED, at=later))
        await FillRepository(db).record(run.run_id, fill("f-b", "o-b", at=later + timedelta(seconds=5)))
        await TradeRepository(db).record(run.run_id, filled, strategy="elephant")

    found = await _window(sessions)

    assert len(found) == 1 and found[0].pnl == float(filled.pnl)


async def test_decisao_aprovada_soma_as_saidas_da_propria_posicao_e_nao_da_seguinte(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    run = new_run()
    second = T0 + timedelta(hours=6)
    legs = [
        _leg("p1", T0 + timedelta(seconds=5), "110", 1),
        _leg("p2", T0 + timedelta(seconds=5), "90", 2),
        _leg("n1", second + timedelta(seconds=5), "90", 1),
    ]
    async with sessions() as db, db.begin():
        await RunRepository(db).start(run)
        await DecisionRepository(db).record(run.run_id, _approved(T0, "o-a", "a"))
        orders, fills = OrderRepository(db), FillRepository(db)
        await orders.upsert(run.run_id, order("o-a", status=OrderStatus.FILLED))
        await fills.record(run.run_id, fill("f-a", "o-a", at=T0 + timedelta(seconds=5)))
        await orders.upsert(run.run_id, order("o-next", status=OrderStatus.FILLED, at=second))
        await fills.record(run.run_id, fill("f-n", "o-next", at=second + timedelta(seconds=5)))
        for leg in legs:
            await TradeRepository(db).record(run.run_id, leg, strategy="elephant")

    [found] = await _window(sessions)

    assert found.pnl == float(legs[0].pnl + legs[1].pnl)


async def test_vetos_leem_so_codigos_de_ia_dentro_da_janela_com_velas_do_sinal(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    run = new_run()
    signal = Signal(
        symbol=SYMBOL,
        timeframe=Timeframe.H1,
        created_at=T0,
        strategy="elephant",
        entry=Decimal(100),
        stop_loss=Decimal(95),
        take_profit=Decimal(110),
    )
    veto = decision("ai_veto", traded=False, at=T0).model_copy(update={"signal": signal, "insight": AI})
    other = decision("no_signal", traded=False, at=T0 + timedelta(hours=1))
    candles = [
        Candle(
            symbol=SYMBOL,
            timeframe=Timeframe.H1,
            open_time=T0 + timedelta(hours=i),
            open=Decimal(100),
            high=Decimal(101),
            low=Decimal(99),
            close=Decimal(100),
            volume=Decimal(10),
            closed=True,
        )
        for i in range(-1, 6)
    ]
    async with sessions() as db, db.begin():
        await RunRepository(db).start(run)
        await DecisionRepository(db).record(run.run_id, veto)
        await DecisionRepository(db).record(run.run_id, other)
    await CandleRepository(sessions).upsert_many(candles)
    async with sessions() as db:
        repo = AiValueRepository(db, BacktestSettings())
        found = await repo.vetoes(T0 - timedelta(days=1), T0 + timedelta(days=1))
        outside = await repo.vetoes(T0 + timedelta(days=2), T0 + timedelta(days=3))

    assert [(v.code, v.signal.entry, len(v.candles) > 0) for v in found] == [("ai_veto", Decimal(100), True)]
    assert outside == []
