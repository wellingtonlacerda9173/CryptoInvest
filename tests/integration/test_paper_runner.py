"""Runner do paper: ciclo completo e recuperacao no Timescale (mercado fake) e smoke na Binance publica."""

import asyncio
import socket
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio
from redis import asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from engine.application.risk import EventCalendar
from engine.domain.journal import ControlAction
from engine.domain.order import OrderStatus
from engine.domain.types import Timeframe
from engine.infrastructure.binance.market_data import BinanceMarketData
from engine.infrastructure.config.settings import Settings
from engine.infrastructure.paper import SystemClock
from engine.infrastructure.persistence.unit_of_work import UnitOfWork
from engine.runner.backtest import build_strategies
from engine.runner.paper_config import DEFAULT_WS_URL, PaperRunConfig
from engine.runner.paper_events import RedisEventBus, channel_name
from engine.runner.paper_journal import SqlJournal
from engine.runner.paper_robot import BotState, PaperRobot
from tests.unit.runner.fakes import FakeFeed, Harness, RecordingBus, candle_at, config, rules

pytestmark = pytest.mark.integration

SIGNAL = ("100", "100.5", "99.5", "100")
FILL = ("100", "101", "99.5", "100.5")
STOP = ("100.5", "100.6", "94", "95")
ENTRY = "e2601020600BTC"


def _reachable(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=3):
            return True
    except OSError:
        return False


@pytest_asyncio.fixture
async def journal(scratch_sessions: async_sessionmaker[AsyncSession]) -> SqlJournal:
    return SqlJournal(scratch_sessions, f"it-{uuid4().hex[:12]}")


async def test_full_cycle_is_persisted_atomically_and_recovers(
    journal: SqlJournal, scratch_sessions: async_sessionmaker[AsyncSession]
) -> None:
    h = await Harness(journal=journal).start()
    await h.close(candle_at(29, *SIGNAL))
    await h.close(candle_at(30, *FILL))

    recovery = await journal.load()
    assert [p.symbol for p in recovery.open_positions] == ["BTC/USDT"]
    assert recovery.risk_state is not None
    assert recovery.gateway_state is not None
    assert {o.client_order_id for o in recovery.open_orders} == {f"{ENTRY}:oco", f"{ENTRY}:partial"}

    revived = await Harness(
        journal=journal, history=h.history, feed=FakeFeed(h.feed.candles), clock=h.clock
    ).start()
    assert revived.robot.live_symbols == ["BTC/USDT"]
    [oco] = revived.robot.gateway.open_protective_state()
    assert oco.stop == Decimal(95)
    await revived.close(candle_at(31, *STOP))

    async with UnitOfWork(scratch_sessions) as uow:
        [trade] = await uow.trades.list_for_run(journal.run_id)
        assert trade.exit_reason.value == "stop_loss"
        assert await uow.positions.list_open(journal.run_id) == []
        oco_order = await uow.orders.get(journal.run_id, f"{ENTRY}:oco")
        assert oco_order is not None
        assert oco_order.status is OrderStatus.FILLED
        assert len(await uow.equity.list_for_run(journal.run_id)) >= 3
        actions = [e.action for e in await uow.control_events.list_for_run(journal.run_id)]
        assert actions.count(ControlAction.START) == 2
        decisions = await uow.decisions.list_recent(journal.run_id)
        assert {"approved", "no_signal"} <= {d.code for d in decisions}
        saved = await uow.risk_states.load_latest(journal.run_id)
        assert saved is not None
        assert saved.equity == revived.robot.state.equity


async def test_exit_is_all_or_nothing_when_the_state_write_fails(
    journal: SqlJournal,
    scratch_sessions: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = await Harness(journal=journal).start()
    await h.close(candle_at(29, *SIGNAL))
    await h.close(candle_at(30, *FILL))

    async def broken(*_: object, **__: object) -> int:
        raise RuntimeError("falha injetada ao gravar o estado")

    monkeypatch.setattr("engine.infrastructure.persistence.state_repository.RiskStateRepository.save", broken)
    with pytest.raises(RuntimeError, match="falha injetada"):
        await h.close(candle_at(31, *STOP))
    monkeypatch.undo()

    async with UnitOfWork(scratch_sessions) as uow:
        assert await uow.trades.list_for_run(journal.run_id) == []
        assert len(await uow.positions.list_open(journal.run_id)) == 1
        oco_order = await uow.orders.get(journal.run_id, f"{ENTRY}:oco")
        assert oco_order is not None
        assert oco_order.status is OrderStatus.NEW


async def test_redis_bus_publishes_and_degrades_without_breaking(settings: Settings) -> None:
    if not _reachable("localhost", 6379):
        pytest.skip("Redis indisponivel")
    bus = RedisEventBus(settings.redis_url)
    client = aioredis.from_url(settings.redis_url, decode_responses=True)
    pubsub = client.pubsub()
    await pubsub.subscribe(channel_name("heartbeat"))
    await pubsub.get_message(timeout=1)
    await bus.publish("heartbeat", {"run_id": "x", "state": "RUNNING"})
    message = None
    for _ in range(20):
        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.5)
        if message:
            break
    assert message is not None
    assert '"RUNNING"' in message["data"]
    await pubsub.aclose()
    await client.aclose()
    await bus.close()

    dead = RedisEventBus("redis://127.0.0.1:1/0")
    await dead.publish("state", {"x": 1})
    await dead.close()
    assert dead.healthy is False
    assert dead.failures == 1


@pytest_asyncio.fixture
async def stream_reachable() -> AsyncIterator[None]:
    if not (_reachable("stream.binance.com", 443) and _reachable("api.binance.com", 443)):
        pytest.skip("Binance publica inacessivel")
    yield


@pytest.mark.usefixtures("stream_reachable")
async def test_smoke_runs_against_binance_persists_heartbeat_and_stops_clean(
    journal: SqlJournal, scratch_sessions: async_sessionmaker[AsyncSession]
) -> None:
    """~80 s ao vivo (1m para haver vela fechada): decisoes, heartbeat, equity e parada limpa."""
    cfg = PaperRunConfig(
        symbols=("BTC/USDT",),
        timeframe=Timeframe.M1,
        allow_open_calendar=True,
        heartbeat_seconds=5,
        equity_seconds=10,
        history_window=120,
    )
    bus = RecordingBus()
    async with BinanceMarketData(ws_url=DEFAULT_WS_URL) as market:
        robot = PaperRobot(
            cfg,
            run_id=journal.run_id,
            journal=journal,
            bus=bus,
            market=market,
            strategies=build_strategies(cfg.strategies, cfg.params),
            rules={"BTC/USDT": rules()["BTC/USDT"]},
            calendar=EventCalendar(),
            clock=SystemClock(),
            max_runtime=timedelta(seconds=80),
        )
        started = datetime.now(UTC)
        await robot.start()
        async with asyncio.timeout(150):
            await robot.run()
        await robot.shutdown()

    assert robot.bot_state is BotState.STOPPED
    assert datetime.now(UTC) - started >= timedelta(seconds=75)
    async with UnitOfWork(scratch_sessions) as uow:
        assert len(await uow.equity.list_for_run(journal.run_id)) >= 3
        decisions = await uow.decisions.list_recent(journal.run_id)
        assert decisions, "a vela de 1m fechada durante o smoke gera ao menos uma decisao"
        actions = [e.action for e in await uow.control_events.list_for_run(journal.run_id)]
        assert actions[0] is ControlAction.START
        assert actions[-1] is ControlAction.STOP
        assert await uow.positions.list_open(journal.run_id) == []
        assert await uow.risk_states.load_latest(journal.run_id) is not None
    assert len(bus.of("heartbeat")) >= 5
    assert bus.of("equity")


def test_config_hash_ignores_operational_fields() -> None:
    base = config()
    assert base.config_hash() == config(heartbeat_seconds=99, allow_open_calendar=False).config_hash()
    assert base.config_hash() != config(capital=Decimal(5000)).config_hash()
