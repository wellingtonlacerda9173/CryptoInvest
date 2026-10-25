"""Migracao 0012, auditoria `ai_calls` e PaperRobot com analista fake, em bancos descartaveis `*_test`."""

import asyncio
import socket
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import URL, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from engine.application.ai.ledger import AiCallRecord
from engine.application.trading_config.document import AiConfig
from engine.infrastructure.ai.analyst import AnthropicAnalyst
from engine.infrastructure.ai.client import LlmError
from engine.infrastructure.config.settings import Settings
from engine.infrastructure.persistence.ai_call_repository import SqlAiCallStore
from engine.infrastructure.persistence.session import create_engine, create_session_factory
from engine.runner.paper_journal import SqlJournal
from tests.integration.disposable_db import (
    RoleSetup,
    assert_disposable_database,
    disposable_database,
    restricted_roles,
)
from tests.integration.journal_helpers import new_run
from tests.unit.ai.support import FakeClient, good, no_sleep, opinion
from tests.unit.runner.fakes import Harness, candle_at, config
from tests.unit.runner.test_paper_ai import FakeAnalyst

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
MONTH = datetime(2026, 10, 1, tzinfo=UTC)
HASH = "a" * 64
SIGNAL_CANDLE = ("100", "100.5", "99.5", "100")


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
    config_ = Config("alembic.ini")
    config_.attributes["database_url"] = url
    return config_


@pytest.fixture
def head_url(db_settings: Settings) -> Iterator[URL]:
    with disposable_database(db_settings, "aicalls") as url:
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


def call(result: str = "ok", cost: str = "0.01", at: datetime = NOW, key: str | None = "k") -> AiCallRecord:
    return AiCallRecord(
        called_at=at,
        model="claude-haiku-5-5",
        prompt_version="analyst-v1",
        prompt_hash=HASH,
        response_hash=HASH,
        response_text="{}",
        input_tokens=10,
        output_tokens=5,
        cost_usd=Decimal(cost),
        latency_ms=120,
        result=result,  # type: ignore[arg-type]
        decision_key=key,
    )


def test_migration_0012_up_down_up_and_alembic_check(db_settings: Settings) -> None:
    async def exists(url: URL) -> bool:
        eng = create_engine(url)
        try:
            async with eng.connect() as conn:
                return await conn.scalar(text("SELECT to_regclass('public.ai_calls')")) is not None
        finally:
            await eng.dispose()

    with disposable_database(db_settings, "mig12") as url:
        cfg = alembic_config(url)
        command.upgrade(cfg, "0011")
        assert not asyncio.run(exists(url))
        command.upgrade(cfg, "head")
        assert asyncio.run(exists(url))
        command.check(cfg)
        command.downgrade(cfg, "0011")
        assert not asyncio.run(exists(url))
        command.upgrade(cfg, "head")
        command.check(cfg)
        assert asyncio.run(exists(url))


async def test_store_roundtrip_and_month_usage(sessions: async_sessionmaker[AsyncSession]) -> None:
    store = SqlAiCallStore(sessions)
    await store.record(call("ok", "0.50"))
    await store.record(call("invalid", "0.25"))
    await store.record(call("budget", "0", key=None))
    await store.record(call("ok", "9", at=datetime(2026, 9, 30, 23, tzinfo=UTC)))

    usage = await store.usage(MONTH)

    assert usage.spent_usd == Decimal("0.75") and usage.calls == 3
    assert usage.by_result == {"ok": 1, "invalid": 1, "budget": 1}


async def test_table_is_append_only_and_checks_result(
    sessions: async_sessionmaker[AsyncSession], engine: AsyncEngine
) -> None:
    await SqlAiCallStore(sessions).record(call())
    for statement in ("UPDATE ai_calls SET cost_usd = 0", "DELETE FROM ai_calls", "TRUNCATE ai_calls"):
        with pytest.raises(DBAPIError, match=r"append-only|forbid|nao"):
            async with engine.begin() as conn:
                await conn.execute(text(statement))
    with pytest.raises(IntegrityError, match="ck_ai_calls_result"):
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO ai_calls (called_at, model, prompt_version, prompt_hash, input_tokens, "
                    "output_tokens, cost_usd, latency_ms, result) "
                    "VALUES (now(), 'm', 'v', 'h', 0, 0, 0, 0, 'x')"
                )
            )


def test_roles_robot_inserts_and_reads_api_only_reads(db_settings: Settings) -> None:
    with disposable_database(db_settings, "airoles") as url:
        command.upgrade(alembic_config(url), "head")
        with restricted_roles(db_settings, url) as roles:
            asyncio.run(check_roles(roles))


async def check_roles(roles: RoleSetup) -> None:
    app, api = create_engine(roles.app_url), create_engine(roles.api_url)
    try:
        store = SqlAiCallStore(create_session_factory(app))
        await store.record(call())
        assert (await store.usage(MONTH)).calls == 1
        for statement in (
            "UPDATE ai_calls SET cost_usd = 0",
            "DELETE FROM ai_calls",
            "TRUNCATE ai_calls",
            "ALTER TABLE ai_calls DISABLE TRIGGER ai_calls_append_only",
        ):
            with pytest.raises(DBAPIError, match=r"permission denied|must be owner"):
                async with app.begin() as conn:
                    await conn.execute(text(statement))
        api_store = SqlAiCallStore(create_session_factory(api))
        assert (await api_store.usage(MONTH)).calls == 1
        with pytest.raises(DBAPIError, match="permission denied"):
            await api_store.record(call())
    finally:
        await app.dispose()
        await api.dispose()


# ---------------------------------------------------------------- PaperRobot + banco


async def run_robot(
    sessions: async_sessionmaker[AsyncSession], analyst: object, *, enabled: bool = True
) -> tuple[Harness, SqlJournal]:
    journal = SqlJournal(sessions, new_run().run_id)
    h = Harness(cfg=config(ai=AiConfig(enabled=enabled)), journal=journal, analyst=analyst)
    await h.start()
    await h.close(candle_at(29, *SIGNAL_CANDLE))
    await h.robot.shutdown("fim do teste")
    return h, journal


async def stored_decisions(engine: AsyncEngine, run_id: str) -> list[tuple[str, bool, dict[str, object]]]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT code, traded, signal FROM decisions "
                "WHERE run_id = :r AND code <> 'shutdown' ORDER BY id"
            ),
            {"r": run_id},
        )
        return [(r.code, r.traded, r.signal or {}) for r in rows]


async def test_veto_persists_ai_veto_with_the_opinion_for_the_panel(
    sessions: async_sessionmaker[AsyncSession], engine: AsyncEngine
) -> None:
    _, journal = await run_robot(sessions, FakeAnalyst(opinion("veto", 0.9, "exaustao")))
    [(code, traded, signal)] = await stored_decisions(engine, journal.run_id)
    assert (code, traded) == ("ai_veto", False)
    assert str(signal["ai_opinion"]).startswith("Vetou") and "entry" in signal


async def test_approval_persists_the_opinion_and_trades(
    sessions: async_sessionmaker[AsyncSession], engine: AsyncEngine
) -> None:
    _, journal = await run_robot(sessions, FakeAnalyst(opinion("approve", 0.6, "coerente")))
    [(code, traded, signal)] = await stored_decisions(engine, journal.run_id)
    assert (code, traded) == ("approved", True) and str(signal["ai_opinion"]).startswith("Aprovou")


async def test_no_opinion_and_disabled_match_the_run_without_ai(
    sessions: async_sessionmaker[AsyncSession], engine: AsyncEngine
) -> None:
    _, none_run = await run_robot(sessions, FakeAnalyst(None))
    _, off_run = await run_robot(sessions, FakeAnalyst(opinion("veto")), enabled=False)
    for journal in (none_run, off_run):
        [(code, traded, signal)] = await stored_decisions(engine, journal.run_id)
        assert (code, traded) == ("approved", True) and "ai_opinion" not in signal


async def test_unavailable_ai_audits_the_failure_and_the_robot_trades_as_without_ai(
    sessions: async_sessionmaker[AsyncSession], engine: AsyncEngine
) -> None:
    store = SqlAiCallStore(sessions)
    client = FakeClient(lambda _: LlmError("APIConnectionError", retryable=True))
    analyst = AnthropicAnalyst(
        client,
        store,
        model="claude-haiku-5-5",
        monthly_budget_usd=Decimal(20),
        timeout_seconds=2,
        sleep=no_sleep,
    )
    _, journal = await run_robot(sessions, analyst)

    [(code, traded, _)] = await stored_decisions(engine, journal.run_id)
    assert (code, traded) == ("approved", True)
    async with engine.connect() as conn:
        [(result, key)] = (
            await conn.execute(text("SELECT result, decision_key FROM ai_calls WHERE result <> 'reserve'"))
        ).all()
    assert result == "error" and key.startswith("scripted:BTC/USDT:")


async def test_exhausted_budget_makes_no_call_records_budget_and_trades_as_without_ai(
    sessions: async_sessionmaker[AsyncSession], engine: AsyncEngine
) -> None:
    client = FakeClient(lambda _: good())
    analyst = AnthropicAnalyst(
        client,
        SqlAiCallStore(sessions),
        model="claude-haiku-5-5",
        monthly_budget_usd=Decimal(0),
        timeout_seconds=2,
        sleep=no_sleep,
    )
    _, journal = await run_robot(sessions, analyst)

    [(code, traded, _)] = await stored_decisions(engine, journal.run_id)
    assert (code, traded) == ("approved", True) and client.requests == []
    async with engine.connect() as conn:
        assert (await conn.execute(text("SELECT result FROM ai_calls"))).scalars().all() == ["budget"]


async def test_real_analyst_veto_is_audited_end_to_end(
    sessions: async_sessionmaker[AsyncSession], engine: AsyncEngine
) -> None:
    reply = '{"verdict":"veto","confidence":0.9,"reason":"entrada contra a tendencia"}'
    analyst = AnthropicAnalyst(
        FakeClient(lambda _: good(reply)),
        SqlAiCallStore(sessions),
        model="claude-haiku-5-5",
        monthly_budget_usd=Decimal(20),
        timeout_seconds=2,
        sleep=no_sleep,
    )
    _, journal = await run_robot(sessions, analyst)

    [(code, traded, _)] = await stored_decisions(engine, journal.run_id)
    assert (code, traded) == ("ai_veto", False)
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT result, input_tokens, cost_usd, prompt_hash "
                    "FROM ai_calls WHERE result <> 'reserve'"
                )
            )
        ).one()
        linked = await conn.scalar(
            text(
                "SELECT count(*) FROM ai_calls s JOIN ai_calls r ON r.id = s.reservation_id "
                "WHERE r.result = 'reserve' AND s.result = 'ok'"
            )
        )
    assert row.result == "ok" and row.input_tokens == 1500 and row.cost_usd > 0 and len(row.prompt_hash) == 64
    assert linked == 1


async def test_reserva_conta_como_gasto_ate_liquidar_e_nunca_passa_do_teto(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    store = SqlAiCallStore(sessions)
    reserve = call("reserve", "0.40", key=None)
    first = await store.reserve(reserve, budget=Decimal("1"), month_start=MONTH)
    second = await store.reserve(reserve, budget=Decimal("1"), month_start=MONTH)
    refused = await store.reserve(reserve, budget=Decimal("1"), month_start=MONTH)

    assert first is not None and second is not None and refused is None
    open_usage = await store.usage(MONTH)
    assert open_usage.spent_usd == Decimal("0.80") and open_usage.calls == 0

    await store.settle(call("ok", "0.05"), first)
    settled = await store.usage(MONTH)
    assert settled.spent_usd == Decimal("0.45") and settled.by_result == {"ok": 1}
    with pytest.raises(IntegrityError, match="ux_ai_calls_reservation"):
        await store.settle(call("ok", "0.05"), first)


async def test_dois_processos_reservando_ao_mesmo_tempo_respeitam_o_teto(
    sessions: async_sessionmaker[AsyncSession], head_url: URL
) -> None:
    other = create_engine(head_url)
    try:
        stores = [SqlAiCallStore(sessions), SqlAiCallStore(create_session_factory(other))]
        reserve = call("reserve", "0.30", key=None)
        ids = await asyncio.gather(
            *(stores[i % 2].reserve(reserve, budget=Decimal("1"), month_start=MONTH) for i in range(12))
        )
        assert sum(1 for i in ids if i is not None) == 3
        assert (await stores[0].usage(MONTH)).spent_usd == Decimal("0.90")
    finally:
        await other.dispose()


async def test_analista_cancelado_no_meio_deixa_reserva_liquidada_como_cancelled(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    started = asyncio.Event()

    class Slow(FakeClient):
        async def complete(self, **kw: object) -> object:  # type: ignore[override]
            started.set()
            await asyncio.sleep(5)
            return good()

    store = SqlAiCallStore(sessions)
    analyst = AnthropicAnalyst(
        Slow(lambda _: good()),
        store,
        model="claude-haiku-5-5",
        monthly_budget_usd=Decimal(20),
        timeout_seconds=10.0,
        sleep=no_sleep,
    )
    from tests.unit.ai.support import make_request

    task = asyncio.create_task(analyst.analyze(make_request()))
    await asyncio.wait_for(started.wait(), 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    usage = await store.usage(datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0))
    assert usage.by_result == {"cancelled": 1} and usage.spent_usd > 0
