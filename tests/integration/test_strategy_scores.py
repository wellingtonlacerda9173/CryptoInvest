"""Migracao 0011, repositorio e papeis de `strategy_scores`: tudo em bancos descartaveis `*_test`."""

import asyncio
import socket
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import URL, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from engine.application.regime import Regime
from engine.application.selector import SelectorConfig, TradeSample, build_scores
from engine.domain.journal import DecisionEntry
from engine.infrastructure.config.settings import Settings
from engine.infrastructure.persistence.score_repository import ScoreRepository
from engine.infrastructure.persistence.session import create_engine, create_session_factory
from engine.infrastructure.persistence.unit_of_work import UnitOfWork
from engine.runner.selector_store import SqlScoreStore
from tests.integration.disposable_db import (
    RoleSetup,
    assert_disposable_database,
    disposable_database,
    restricted_roles,
)
from tests.integration.journal_helpers import SYMBOL, T0, decision, new_run, trade

pytestmark = pytest.mark.integration

STRATEGY = "ema_cross_trend"
AS_OF = datetime(2026, 6, 1, tzinfo=UTC)


@pytest.fixture(scope="module")
def db_settings() -> Settings:
    from engine.infrastructure.config.settings import load_settings

    try:
        loaded = load_settings()
    except Exception as exc:
        pytest.skip(f"settings indisponiveis: {exc.__class__.__name__}")
    try:
        with socket.create_connection((loaded.database_host, loaded.database_port), timeout=3):
            pass
    except OSError:
        pytest.skip("Postgres/Timescale indisponivel (docker compose -f docker-compose.dev.yml up -d)")
    return loaded


def alembic_config(url: URL) -> Config:
    assert_disposable_database(url)
    config = Config("alembic.ini")
    config.attributes["database_url"] = url
    return config


async def table_exists(engine: AsyncEngine) -> bool:
    async with engine.connect() as conn:
        found = await conn.scalar(text("SELECT to_regclass('public.strategy_scores')"))
    return found is not None


@pytest.fixture
def head_url(db_settings: Settings) -> Iterator[URL]:
    with disposable_database(db_settings, "scores") as url:
        command.upgrade(alembic_config(url), "head")
        yield url


@pytest_asyncio.fixture
async def engine(head_url: URL) -> AsyncIterator[AsyncEngine]:
    eng = create_engine(head_url)
    yield eng
    await eng.dispose()


@pytest.fixture
def sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(engine)


def pool(
    strategy: str, symbol: str, mean_r: float, regime: Regime = Regime.TENDENCIA_ALTA
) -> list[TradeSample]:
    out = []
    for k in range(40):
        end = AS_OF - timedelta(days=1, hours=k)
        out.append(
            TradeSample(
                strategy=strategy,
                symbol=symbol,
                regime=regime,
                opened_at=end - timedelta(hours=3),
                closed_at=end,
                r_multiple=mean_r + (0.5 if k % 2 else -0.5),
            )
        )
    return out


def scores(symbol: str, mean_r: float = 1.0):  # type: ignore[no-untyped-def]
    return list(build_scores(pool(STRATEGY, symbol, mean_r), AS_OF, SelectorConfig()).values())


def test_migration_0011_up_down_up_and_alembic_check(db_settings: Settings) -> None:
    async def exists(url: URL) -> bool:
        engine = create_engine(url)
        try:
            return await table_exists(engine)
        finally:
            await engine.dispose()

    with disposable_database(db_settings, "mig11") as url:
        config = alembic_config(url)
        command.upgrade(config, "0010")
        assert not asyncio.run(exists(url))
        command.upgrade(config, "head")
        assert asyncio.run(exists(url))
        command.check(config)
        command.downgrade(config, "0010")
        assert not asyncio.run(exists(url))
        command.upgrade(config, "head")
        command.check(config)
        assert asyncio.run(exists(url))


async def test_table_is_append_only_by_trigger(
    sessions: async_sessionmaker[AsyncSession], engine: AsyncEngine
) -> None:
    async with sessions() as session, session.begin():
        await ScoreRepository(session).publish(
            scores("BTC/USDT"), timeframe="1h", origin="backtest", calculated_at=AS_OF
        )
    for statement in (
        "UPDATE strategy_scores SET score = 0",
        "DELETE FROM strategy_scores",
        "TRUNCATE strategy_scores",
    ):
        with pytest.raises(DBAPIError, match="append-only"):
            async with engine.begin() as conn:
                await conn.execute(text(statement))


async def test_origin_is_restricted_and_snapshot_key_is_unique(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    items = scores("BTC/USDT")
    with pytest.raises(IntegrityError, match="ck_strategy_scores_origin"):
        async with sessions() as session, session.begin():
            await ScoreRepository(session).publish(
                items,
                timeframe="1h",
                origin="manual",  # type: ignore[arg-type]
                calculated_at=AS_OF,
            )
    with pytest.raises(IntegrityError, match="uq_strategy_scores_snapshot_key"):
        async with sessions() as session, session.begin():
            repo = ScoreRepository(session)
            await repo.publish(items, timeframe="1h", origin="paper", calculated_at=AS_OF, snapshot_id="s1")
            await repo.publish(items, timeframe="1h", origin="paper", calculated_at=AS_OF, snapshot_id="s1")


async def test_latest_returns_the_newest_snapshot_per_symbol_and_timeframe(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    store = SqlScoreStore(sessions)
    older = scores("BTC/USDT", 0.2) + scores("ETH/USDT", 0.2)
    newer = scores("BTC/USDT", 1.5)
    await store.publish(older, timeframe="1h", origin="backtest", calculated_at=AS_OF)
    await store.publish(
        newer, timeframe="1h", origin="paper", calculated_at=AS_OF + timedelta(hours=6), run_id=None
    )
    await store.publish(scores("BTC/USDT", 9.0), timeframe="4h", origin="backtest", calculated_at=AS_OF)

    latest = await store.latest_scores("1h", ["BTC/USDT", "ETH/USDT"])

    btc = latest[(STRATEGY, Regime.TENDENCIA_ALTA.value, "BTC/USDT")]
    eth = latest[(STRATEGY, Regime.TENDENCIA_ALTA.value, "ETH/USDT")]
    assert btc.mean_r == pytest.approx(1.5, abs=0.01) and eth.mean_r == pytest.approx(0.2, abs=0.01)
    assert btc.trades == 40 and btc.eligible
    assert await store.latest_scores("1d", ["BTC/USDT"]) == {}
    only_eth = await store.latest_scores("1h", ["ETH/USDT"])
    assert {k[2] for k in only_eth} == {"ETH/USDT"}


async def test_journal_samples_label_round_trips_with_the_entry_regime(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    run = new_run()
    entry = decision(traded=True).model_copy(
        update={
            "strategy": STRATEGY,
            "decided_at": T0 - timedelta(minutes=1),
            "insight": {"regime": "tendência de alta", "regime_code": "tendencia_alta", "scores": []},
        }
    )
    async with UnitOfWork(sessions) as uow:
        await uow.runs.start(run)
        await uow.decisions.record(run.run_id, entry)
        await uow.trades.record(
            run.run_id, trade("a", exit_price="110"), strategy=STRATEGY, risk_amount=Decimal(10)
        )
        await uow.trades.record(
            run.run_id,
            trade("b", exit_price="104", closed=T0 + timedelta(hours=5)),
            strategy=STRATEGY,
            risk_amount=Decimal(10),
        )
    async with sessions() as session:
        samples = await ScoreRepository(session).journal_samples([SYMBOL])
        stored = (await DecisionRepositoryProbe.read(session, run.run_id))[0]

    [sample] = samples
    assert sample.regime is Regime.TENDENCIA_ALTA and sample.strategy == STRATEGY
    assert sample.closed_at == T0 + timedelta(hours=5)
    assert sample.r_multiple == pytest.approx(
        float((Decimal(20) - Decimal("0.4")) / 10 + (Decimal(8) - Decimal("0.4")) / 10)
    )
    assert sample.source == "paper"
    assert stored.insight is not None and stored.insight["regime_code"] == "tendencia_alta"
    assert stored.signal is None


class DecisionRepositoryProbe:
    @staticmethod
    async def read(session: AsyncSession, run_id: str) -> list[DecisionEntry]:
        from engine.infrastructure.persistence.decision_repository import DecisionRepository

        return await DecisionRepository(session).list_recent(run_id)


async def test_trades_without_a_regime_label_are_not_used(sessions: async_sessionmaker[AsyncSession]) -> None:
    run = new_run()
    async with UnitOfWork(sessions) as uow:
        await uow.runs.start(run)
        await uow.decisions.record(run.run_id, decision(traded=True))
        await uow.trades.record(
            run.run_id, trade("c", exit_price="110"), strategy=STRATEGY, risk_amount=Decimal(10)
        )
    async with sessions() as session:
        assert await ScoreRepository(session).journal_samples([SYMBOL]) == []


def test_robot_role_can_insert_and_read_but_not_change_scores(db_settings: Settings) -> None:
    with disposable_database(db_settings, "scoreroles") as url:
        command.upgrade(alembic_config(url), "head")
        with restricted_roles(db_settings, url) as roles:
            asyncio.run(check_roles(roles))


async def check_roles(roles: RoleSetup) -> None:
    app, api = create_engine(roles.app_url), create_engine(roles.api_url)
    try:
        store = SqlScoreStore(create_session_factory(app))
        await store.publish(scores("BTC/USDT"), timeframe="1h", origin="backtest", calculated_at=AS_OF)
        assert await store.latest_scores("1h", ["BTC/USDT"])
        for statement in (
            "UPDATE strategy_scores SET score = 0",
            "DELETE FROM strategy_scores",
            "TRUNCATE strategy_scores",
            "ALTER TABLE strategy_scores DISABLE TRIGGER strategy_scores_append_only",
        ):
            with pytest.raises(DBAPIError, match=r"permission denied|must be owner"):
                async with app.begin() as conn:
                    await conn.execute(text(statement))
        async with api.connect() as conn:
            assert await conn.scalar(text("SELECT count(*) FROM strategy_scores")) > 0
        with pytest.raises(DBAPIError, match="permission denied"):
            async with api.begin() as conn:
                await conn.execute(text("DELETE FROM strategy_scores"))
        with pytest.raises(DBAPIError, match="permission denied"):
            await SqlScoreStore(create_session_factory(api)).publish(
                scores("ETH/USDT"), timeframe="1h", origin="paper", calculated_at=AS_OF
            )
    finally:
        await app.dispose()
        await api.dispose()


async def test_paper_robot_with_selector_persists_the_insight_the_panel_reads(
    sessions: async_sessionmaker[AsyncSession], engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from engine.application.selector import SelectorSettings
    from engine.runner import paper_robot
    from engine.runner.paper_journal import SqlJournal
    from tests.unit.backtest.engine_helpers import Scripted
    from tests.unit.runner.fakes import Harness, candle_at, config
    from tests.unit.runner.test_paper_selector import force_regime

    force_regime(monkeypatch, Regime.TENDENCIA_ALTA)
    assert paper_robot.classify_regime is not None
    mine = [
        TradeSample(
            strategy="scripted",
            symbol=SYMBOL,
            regime=Regime.TENDENCIA_ALTA,
            opened_at=T0 - timedelta(days=1, hours=k + 3),
            closed_at=T0 - timedelta(days=1, hours=k),
            r_multiple=1.0 + (0.5 if k % 2 else -0.5),
        )
        for k in range(40)
    ]
    store = SqlScoreStore(sessions)
    await store.publish(
        list(build_scores(mine, T0, SelectorConfig()).values()),
        timeframe="1h",
        origin="backtest",
        calculated_at=T0,
    )
    journal = SqlJournal(sessions, new_run().run_id)
    h = Harness(
        cfg=config(selector=SelectorSettings(enabled=True)),
        journal=journal,
        strategies=[Scripted({29})],
        score_store=store,
    )
    await h.start()
    await h.close(candle_at(29, "100", "100.5", "99.5", "100"))
    await h.robot.shutdown("fim do teste")

    async with sessions() as session:
        [row] = [
            r for r in await DecisionRepositoryProbe.read(session, journal.run_id) if r.code == "approved"
        ]
    assert row.traded and row.signal is not None
    assert row.insight is not None and row.insight["ai_opinion"] == ""
    async with engine.connect() as conn:
        stored = (
            await conn.execute(
                text("SELECT signal FROM decisions WHERE run_id = :r AND traded"), {"r": journal.run_id}
            )
        ).scalar_one()
    assert {"regime", "scores", "ai_opinion", "entry", "stop_loss", "take_profit"} <= stored.keys()
    assert stored["regime"] == "tendência de alta" and stored["scores"][0]["strategy"] == "scripted"
