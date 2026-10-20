import asyncio
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import URL, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from engine.application.backtest.models import EquityPoint
from engine.application.risk import RiskState
from engine.domain.journal import ControlAction, ControlEvent, RunMode
from engine.domain.order import OrderStatus
from engine.domain.signal import Signal
from engine.domain.types import Timeframe
from engine.infrastructure.persistence.recovery import load_recovery_state
from engine.infrastructure.persistence.serialization import PersistenceError
from engine.infrastructure.persistence.session import create_engine
from engine.infrastructure.persistence.trade_repository import TradeConflictError
from engine.infrastructure.persistence.unit_of_work import UnitOfWork
from tests.integration.disposable_db import assert_disposable_database
from tests.integration.journal_helpers import (
    SYMBOL,
    T0,
    busy_risk_state,
    decision,
    fill,
    new_run,
    order,
    position,
    trade,
)

pytestmark = pytest.mark.integration

D = Decimal
Sessions = async_sessionmaker[AsyncSession]


@pytest.fixture
async def run_id(sessions: Sessions) -> str:
    run = new_run()
    async with UnitOfWork(sessions) as uow:
        await uow.runs.start(run)
    return run.run_id


async def test_run_start_is_idempotent_but_rejects_different_config(sessions: Sessions) -> None:
    run = new_run()
    async with UnitOfWork(sessions) as uow:
        await uow.runs.start(run)
        await uow.runs.start(run)
        await uow.runs.finish(run.run_id, T0 + timedelta(days=1))
        stored = await uow.runs.get(run.run_id)
    assert stored is not None
    assert (stored.mode, stored.ended_at) == (RunMode.PAPER, T0 + timedelta(days=1))

    with pytest.raises(PersistenceError, match="diferentes"):
        async with UnitOfWork(sessions) as uow:
            await uow.runs.start(run.model_copy(update={"config_hash": "outro"}))


async def test_run_mode_is_constrained(engine: AsyncEngine) -> None:
    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await conn.execute(text("INSERT INTO runs VALUES ('bad-mode', 'turbo', now(), NULL, 'h', 'v')"))


async def test_orders_progress_and_ignore_stale_updates(sessions: Sessions, run_id: str) -> None:
    async with UnitOfWork(sessions) as uow:
        await uow.orders.upsert(run_id, order(f"c1-{run_id}"))
        await uow.orders.upsert(run_id, order(f"c2-{run_id}"))
        assert [o.client_order_id for o in await uow.orders.list_open(run_id)] == [
            f"c1-{run_id}",
            f"c2-{run_id}",
        ]

        filled = order(f"c1-{run_id}", status=OrderStatus.FILLED, at=T0 + timedelta(minutes=2)).model_copy(
            update={"filled_quantity": D(2), "average_price": D("100.5"), "exchange_order_id": "ex-1"}
        )
        await uow.orders.upsert(run_id, filled)
        await uow.orders.upsert(
            run_id, order(f"c1-{run_id}", status=OrderStatus.NEW, at=T0 + timedelta(minutes=1))
        )
        stored = await uow.orders.get(run_id, f"c1-{run_id}")

        assert stored == filled
        assert [o.client_order_id for o in await uow.orders.list_open(run_id)] == [f"c2-{run_id}"]


async def test_fill_is_idempotent_and_requires_existing_order(sessions: Sessions, run_id: str) -> None:
    async with UnitOfWork(sessions) as uow:
        await uow.orders.upsert(run_id, order(f"c1-{run_id}"))
        assert await uow.fills.record(run_id, fill("f1", f"c1-{run_id}"))
        assert not await uow.fills.record(run_id, fill("f1", f"c1-{run_id}"))
        assert [f.fill_id for f in await uow.fills.list_for_run(run_id)] == ["f1"]

    with pytest.raises(IntegrityError):
        async with UnitOfWork(sessions) as uow:
            await uow.fills.record(run_id, fill("f2", "inexistente"))


async def test_position_lifecycle_and_stop_never_goes_down(sessions: Sessions, run_id: str) -> None:
    async with UnitOfWork(sessions) as uow:
        await uow.positions.save_open(run_id, position())
        moved = position(
            stop=D(100), highest_price=D(104), breakeven_reached=True, partial_done=True, quantity=D(1)
        )
        await uow.positions.save_open(run_id, moved)
        await uow.positions.save_open(run_id, moved)
        assert await uow.positions.list_open(run_id) == [moved]

    with pytest.raises(PersistenceError, match="stop"):
        async with UnitOfWork(sessions) as uow:
            await uow.positions.save_open(run_id, position(stop=D(97)))

    async with UnitOfWork(sessions) as uow:
        assert await uow.positions.list_open(run_id) == [moved]
        assert await uow.positions.close(run_id, SYMBOL, T0 + timedelta(hours=4))
        assert not await uow.positions.close(run_id, SYMBOL, T0 + timedelta(hours=4))
        assert await uow.positions.list_open(run_id) == []
        await uow.positions.save_open(run_id, position(opened_at=T0 + timedelta(hours=5)))
        assert len(await uow.positions.list_open(run_id)) == 1


async def test_trade_record_is_idempotent_and_computes_r_multiple(
    sessions: Sessions, run_id: str, engine: AsyncEngine
) -> None:
    loser = trade("t1")
    async with UnitOfWork(sessions) as uow:
        assert await uow.trades.record(run_id, loser, strategy="elephant", risk_amount=D(10))
        assert not await uow.trades.record(run_id, loser, strategy="elephant", risk_amount=D(10))
        assert await uow.trades.list_for_run(run_id) == [loser]

    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT pnl, r_multiple FROM trades WHERE run_id = :r AND trade_id = 't1'"),
                {"r": run_id},
            )
        ).one()
    assert (row.pnl, row.r_multiple) == (D("-20.4"), D("-2.04"))


async def test_trade_with_same_id_but_different_content_is_rejected(sessions: Sessions, run_id: str) -> None:
    async with UnitOfWork(sessions) as uow:
        await uow.trades.record(run_id, trade("t1", exit_price="90"))

    with pytest.raises(TradeConflictError):
        async with UnitOfWork(sessions) as uow:
            await uow.trades.record(run_id, trade("t1", exit_price="99"))


async def test_trade_without_id_is_refused(sessions: Sessions, run_id: str) -> None:
    anonymous = trade("x").model_copy(update={"trade_id": None})
    with pytest.raises(ValueError, match="trade_id"):
        async with UnitOfWork(sessions) as uow:
            await uow.trades.record(run_id, anonymous)


async def test_trade_replay_survives_values_beyond_database_scale(sessions: Sessions, run_id: str) -> None:
    messy = trade("t-messy").model_copy(update={"entry_fee": D(1) / D(3), "exit_fee": D(2) / D(7)})
    async with UnitOfWork(sessions) as uow:
        assert await uow.trades.record(run_id, messy)
        assert not await uow.trades.record(run_id, messy)


@pytest.fixture
async def scratch_run_id(scratch_sessions: Sessions) -> str:
    run = new_run()
    async with UnitOfWork(scratch_sessions) as uow:
        await uow.runs.start(run)
    return run.run_id


@pytest.fixture
async def recorded_ids(scratch_sessions: Sessions, scratch_run_id: str) -> dict[str, str]:
    sessions, run_id = scratch_sessions, scratch_run_id
    async with UnitOfWork(sessions) as uow:
        await uow.trades.record(run_id, trade("imm-trade"))
        await uow.decisions.record(run_id, decision())
        await uow.control_events.append(
            run_id, ControlEvent(at=T0, action=ControlAction.START, operator="ana", reason="inicio")
        )
        await uow.record_entry(
            run_id,
            order=order(f"imm-{run_id}", status=OrderStatus.FILLED),
            fill=fill("imm-fill", f"imm-{run_id}"),
            position=position(),
            risk_state=busy_risk_state(),
        )
        await uow.audit(f"actor-{run_id}", "append-only", "probe")
        await uow.equity.record(run_id, EquityPoint(at=T0, equity=D(1000), realized=D(0), in_position=True))
    return {"run_id": run_id, "actor": f"actor-{run_id}"}


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE trades SET pnl = 0 WHERE trade_id = 'imm-trade'",
        "UPDATE trades SET exit_reason = 'manual' WHERE trade_id = 'imm-trade'",
        "DELETE FROM trades WHERE trade_id = 'imm-trade'",
        "TRUNCATE trades",
        "UPDATE decisions SET traded = false WHERE run_id = :run_id",
        "DELETE FROM decisions WHERE run_id = :run_id",
        "TRUNCATE decisions",
        "UPDATE control_events SET reason = 'x' WHERE run_id = :run_id",
        "DELETE FROM control_events WHERE run_id = :run_id",
        "TRUNCATE control_events",
        "UPDATE fills SET price = 1 WHERE run_id = :run_id",
        "DELETE FROM fills WHERE run_id = :run_id",
        "TRUNCATE fills",
        "UPDATE risk_state SET version = 99 WHERE run_id = :run_id",
        "DELETE FROM risk_state WHERE run_id = :run_id",
        "TRUNCATE risk_state",
        "UPDATE audit_log SET action = 'x' WHERE actor = :actor",
        "DELETE FROM audit_log WHERE actor = :actor",
        "TRUNCATE audit_log",
        "UPDATE equity_snapshots SET equity = 1 WHERE run_id = :run_id",
        "DELETE FROM equity_snapshots WHERE run_id = :run_id",
        "TRUNCATE equity_snapshots",
    ],
)
async def test_database_refuses_changes_to_append_only_tables(
    scratch_engine: AsyncEngine, recorded_ids: dict[str, str], statement: str
) -> None:
    with pytest.raises(DBAPIError, match="append-only"):
        async with scratch_engine.begin() as conn:
            await conn.execute(text(statement), recorded_ids)


async def test_append_only_rows_survive_attempts(
    scratch_sessions: Sessions, scratch_engine: AsyncEngine, scratch_run_id: str
) -> None:
    sessions, engine, run_id = scratch_sessions, scratch_engine, scratch_run_id
    async with UnitOfWork(sessions) as uow:
        await uow.trades.record(run_id, trade("keep"))
    with pytest.raises(DBAPIError):
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE trades SET pnl = 999 WHERE run_id = :r AND trade_id = 'keep'"), {"r": run_id}
            )
    async with engine.connect() as conn:
        pnl = (
            await conn.execute(
                text("SELECT pnl FROM trades WHERE run_id = :r AND trade_id = 'keep'"), {"r": run_id}
            )
        ).scalar_one()
    assert pnl == D("-20.4")


async def test_decisions_are_idempotent_linked_to_signal_and_keep_context(
    sessions: Sessions, run_id: str
) -> None:
    signal = Signal(
        symbol=SYMBOL,
        timeframe=Timeframe.H1,
        created_at=T0,
        strategy="elephant",
        entry=D(100),
        stop_loss=D(95),
        take_profit=D(110),
    )
    traded = decision(signal_id=f"sig-1-{run_id}").model_copy(update={"signal": signal})
    skipped = decision(
        "spread_too_wide", traded=False, signal_id=f"sig-2-{run_id}", at=T0 + timedelta(minutes=5)
    )
    async with UnitOfWork(sessions) as uow:
        assert await uow.decisions.record(run_id, traded)
        assert not await uow.decisions.record(run_id, traded)
        assert await uow.decisions.record(run_id, skipped)

        feed = await uow.decisions.list_recent(run_id)
        assert [d.code for d in feed] == ["spread_too_wide", "entered"]
        assert feed[1].signal == signal
        assert feed[1].context == {
            "regime": "tendencia",
            "spread": "0.0004",
            "at": T0.isoformat().replace("+00:00", "Z"),
            "levels": ["95", "110"],
        }
        assert [d.code for d in await uow.decisions.list_for_signal(run_id, f"sig-2-{run_id}")] == [
            "spread_too_wide"
        ]


async def test_decision_context_rejects_naive_datetime(sessions: Sessions, run_id: str) -> None:
    naive = decision().model_copy(update={"context": {"at": datetime(2026, 3, 1)}})
    with pytest.raises(ValueError, match="timezone-aware"):
        async with UnitOfWork(sessions) as uow:
            await uow.decisions.record(run_id, naive)


async def test_risk_state_roundtrip_is_exact_and_versioned(sessions: Sessions, run_id: str) -> None:
    state = busy_risk_state()
    assert state.ledger
    assert state.audit
    assert state.pair_pause_until
    assert state.paused

    async with UnitOfWork(sessions) as uow:
        assert await uow.risk_states.load_latest(run_id) is None
        assert await uow.risk_states.save(run_id, RiskState.initial(D(500), T0)) == 1
        assert await uow.risk_states.save(run_id, state) == 2
        loaded = await uow.risk_states.load_latest(run_id)

    assert loaded == state
    assert loaded is not None
    with pytest.raises(TypeError):
        loaded.pair_pause_until[SYMBOL] = T0  # type: ignore[index]


async def test_risk_state_rejects_naive_timestamp(sessions: Sessions, run_id: str) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        async with UnitOfWork(sessions) as uow:
            await uow.risk_states.save(run_id, busy_risk_state(), saved_at=datetime(2026, 3, 1))


async def test_gateway_state_is_versioned_per_run_and_keeps_decimals(sessions: Sessions, run_id: str) -> None:
    other = new_run()
    async with UnitOfWork(sessions) as uow:
        await uow.runs.start(other)
        await uow.gateway_states.save(run_id, {"cash": D("1000.5"), "seq": 1}, saved_at=T0)
        await uow.gateway_states.save(
            run_id, {"cash": D("900.25"), "seq": 2, "at": T0}, saved_at=T0 + timedelta(minutes=1)
        )
        await uow.gateway_states.save(other.run_id, {"cash": D(1)}, saved_at=T0)
        latest = await uow.gateway_states.load_latest(run_id)
        assert latest is not None
        assert (latest.version, latest.saved_at) == (2, T0 + timedelta(minutes=1))
        assert latest.state["cash"] == "900.25"
        mine = await uow.gateway_states.load_latest(other.run_id)
        assert mine is not None
        assert mine.version == 1


async def test_equity_snapshots_hypertable_idempotent_and_range(
    sessions: Sessions, engine: AsyncEngine, run_id: str
) -> None:
    points = [
        EquityPoint(at=T0 + timedelta(hours=i), equity=D(1000 + i), realized=D(1000), in_position=i % 2 == 0)
        for i in range(5)
    ]
    async with UnitOfWork(sessions) as uow:
        for p in points:
            assert await uow.equity.record(run_id, p)
        assert not await uow.equity.record(run_id, points[0])
        got = await uow.equity.list_for_run(
            run_id, since=T0 + timedelta(hours=1), until=T0 + timedelta(hours=4)
        )
        assert got == points[1:4]
    async with engine.connect() as conn:
        count = await conn.scalar(
            text("SELECT count(*) FROM timescaledb_information.hypertables WHERE hypertable_name = :t"),
            {"t": "equity_snapshots"},
        )
    assert count == 1


async def test_control_events_are_listed_in_order_and_require_identification(
    sessions: Sessions, engine: AsyncEngine, run_id: str
) -> None:
    async with UnitOfWork(sessions) as uow:
        await uow.control_events.append(
            run_id,
            ControlEvent(
                at=T0 + timedelta(hours=1), action=ControlAction.PAUSE, operator="ana", reason="manutencao"
            ),
        )
        await uow.control_events.append(
            run_id, ControlEvent(at=T0, action=ControlAction.START, operator="ana", reason="inicio")
        )
        assert [e.action for e in await uow.control_events.list_for_run(run_id)] == [
            ControlAction.START,
            ControlAction.PAUSE,
        ]

    with pytest.raises(ValidationError):
        ControlEvent(at=T0, action=ControlAction.STOP, operator=" ", reason="x")
    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO control_events (run_id, at, action, operator, reason) "
                    "VALUES (:r, now(), 'stop', ' ', 'x')"
                ),
                {"r": run_id},
            )


async def test_audit_is_written_in_the_same_transaction(
    sessions: Sessions, engine: AsyncEngine, run_id: str
) -> None:
    with pytest.raises(RuntimeError):
        async with UnitOfWork(sessions) as uow:
            await uow.audit(f"actor-{run_id}", "rolled_back", "x", {"v": D("1.5")})
            raise RuntimeError
    async with UnitOfWork(sessions) as uow:
        await uow.audit(f"actor-{run_id}", "kept", "x", {"v": D("1.5"), "at": T0})
    async with engine.connect() as conn:
        actions = (
            (
                await conn.execute(
                    text("SELECT action FROM audit_log WHERE actor = :a"), {"a": f"actor-{run_id}"}
                )
            )
            .scalars()
            .all()
        )
    assert actions == ["kept"]


async def open_position(sessions: Sessions, run_id: str) -> None:
    async with UnitOfWork(sessions) as uow:
        await uow.record_entry(
            run_id,
            order=order(f"buy-1-{run_id}", status=OrderStatus.FILLED),
            fill=fill("f-buy", f"buy-1-{run_id}"),
            position=position(),
        )


async def count(engine: AsyncEngine, table: str, run_id: str) -> int:
    async with engine.connect() as conn:
        return (
            await conn.execute(text(f"SELECT count(*) FROM {table} WHERE run_id = :r"), {"r": run_id})
        ).scalar_one()


async def test_exit_is_applied_atomically(sessions: Sessions, engine: AsyncEngine, run_id: str) -> None:
    await open_position(sessions, run_id)
    state = busy_risk_state()
    loss = trade("exit-1")
    exit_kwargs = {
        "fill": fill("f-sell", f"sell-1-{run_id}", side="sell", price="90", at=loss.closed_at),
        "trade": loss,
        "position": None,
        "risk_state": state,
        "order": order(f"sell-1-{run_id}", side="sell", status=OrderStatus.FILLED),
        "risk_amount": D(10),
    }

    async with UnitOfWork(sessions) as uow:
        assert await uow.record_exit(run_id, **exit_kwargs) == 1  # type: ignore[arg-type]
    async with UnitOfWork(sessions) as uow:
        assert await uow.positions.list_open(run_id) == []
        assert await uow.risk_states.load_latest(run_id) == state
        assert await uow.record_exit(run_id, **exit_kwargs) is None  # type: ignore[arg-type]
    assert await count(engine, "trades", run_id) == 1
    assert await count(engine, "fills", run_id) == 2
    assert await count(engine, "risk_state", run_id) == 1


async def test_failure_mid_exit_rolls_everything_back(
    sessions: Sessions, engine: AsyncEngine, run_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    await open_position(sessions, run_id)
    loss = trade("exit-2")

    async def boom(*args: object, **kwargs: object) -> int:
        raise ConnectionError("queda no meio")

    with pytest.raises(ConnectionError):
        async with UnitOfWork(sessions) as uow:
            monkeypatch.setattr(uow.risk_states, "save", boom)
            await uow.record_exit(
                run_id,
                fill=fill("f-sell-2", f"sell-2-{run_id}", side="sell", price="90", at=loss.closed_at),
                trade=loss,
                position=None,
                risk_state=busy_risk_state(),
                order=order(f"sell-2-{run_id}", side="sell", status=OrderStatus.FILLED),
            )

    assert await count(engine, "trades", run_id) == 0
    assert await count(engine, "fills", run_id) == 1
    assert await count(engine, "risk_state", run_id) == 0
    async with UnitOfWork(sessions) as uow:
        assert len(await uow.positions.list_open(run_id)) == 1
        assert await uow.orders.get(run_id, f"sell-2-{run_id}") is None


async def test_exit_without_open_position_rolls_back_the_trade(
    sessions: Sessions, engine: AsyncEngine, run_id: str
) -> None:
    loss = trade("exit-3")
    with pytest.raises(PersistenceError, match="posicao aberta"):
        async with UnitOfWork(sessions) as uow:
            await uow.orders.upsert(run_id, order(f"sell-3-{run_id}", side="sell"))
            await uow.record_exit(
                run_id,
                fill=fill("f-sell-3", f"sell-3-{run_id}", side="sell", price="90", at=loss.closed_at),
                trade=loss,
                position=None,
                risk_state=busy_risk_state(),
            )
    assert await count(engine, "trades", run_id) == 0


async def test_partial_exit_keeps_position_open_with_new_state(sessions: Sessions, run_id: str) -> None:
    await open_position(sessions, run_id)
    remaining = position(
        quantity=D(1), partial_done=True, stop=D(100), highest_price=D(110), take_profit=None
    )
    gain = trade("partial-1", exit_price="110")
    async with UnitOfWork(sessions) as uow:
        await uow.record_exit(
            run_id,
            fill=fill("f-part", f"sell-p-{run_id}", side="sell", price="110", at=gain.closed_at),
            trade=gain,
            position=remaining,
            risk_state=busy_risk_state(),
            order=order(f"sell-p-{run_id}", side="sell", status=OrderStatus.FILLED),
        )
        assert await uow.positions.list_open(run_id) == [remaining]


async def test_load_recovery_state_returns_everything_needed_to_resume(
    sessions: Sessions, run_id: str
) -> None:
    empty = await load_recovery_state(sessions, "run-que-nao-existe")
    assert (empty.run, empty.risk_state, empty.gateway_state, empty.open_positions, empty.open_orders) == (
        None,
        None,
        None,
        (),
        (),
    )

    state = busy_risk_state()
    async with UnitOfWork(sessions) as uow:
        await uow.record_entry(
            run_id,
            order=order(f"buy-r-{run_id}", status=OrderStatus.FILLED),
            fill=fill("f-r", f"buy-r-{run_id}"),
            position=position(),
            risk_state=state,
        )
        await uow.orders.upsert(run_id, order(f"pending-r-{run_id}"))
        await uow.gateway_states.save(run_id, {"cash": D("9800.5"), "orders": [f"pending-r-{run_id}"]})
        await uow.risk_states.save(
            run_id, state.resume(operator="ana", reason="conferido", now=T0 + timedelta(hours=6))
        )

    recovered = await load_recovery_state(sessions, run_id)

    assert recovered.run is not None
    assert recovered.run.run_id == run_id
    assert recovered.risk_state is not None
    assert not recovered.risk_state.paused
    assert recovered.risk_state.ledger == state.ledger
    assert recovered.gateway_state is not None
    assert recovered.gateway_state.state == {"cash": "9800.5", "orders": [f"pending-r-{run_id}"]}
    assert recovered.open_positions == (position(),)
    assert [o.client_order_id for o in recovered.open_orders] == [f"pending-r-{run_id}"]


@pytest.fixture
def alembic_config() -> Config:
    return Config("alembic.ini")


async def alembic(fn: object, config: Config, *args: str) -> None:
    await asyncio.to_thread(fn, config, *args)  # type: ignore[arg-type]


async def test_alembic_check_reports_no_drift(alembic_config: Config, db_ready: None) -> None:
    await alembic(command.check, alembic_config)


async def test_downgrades_are_reversible(alembic_config: Config, migration_url: URL) -> None:
    assert_disposable_database(migration_url)
    alembic_config.attributes["database_url"] = migration_url
    engine = create_engine(migration_url)

    async def scalar(sql: str) -> object:
        async with engine.connect() as conn:
            return (await conn.execute(text(sql))).scalar_one()

    # so tabelas do schema public: a hypertable replica o trigger de linha em cada chunk (schema interno)
    triggers = (
        "SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND (t.tgname LIKE '%append_only' OR t.tgname LIKE '%no_truncate')"
    )
    try:
        await alembic(command.upgrade, alembic_config, "head")
        assert await scalar(triggers) == 14
        await alembic(command.downgrade, alembic_config, "0004")
        assert await scalar(triggers) == 6
        assert await scalar("SELECT to_regclass('uq_orders_client_order_id')") is not None
        await alembic(command.downgrade, alembic_config, "0003")
        assert await scalar(triggers) == 0
        assert await scalar("SELECT to_regproc('forbid_modification')") is None
        assert await scalar("SELECT to_regclass('trades')") is not None

        await alembic(command.downgrade, alembic_config, "0002")
        for table in ("trades", "decisions", "equity_snapshots", "orders", "runs", "control_events"):
            assert await scalar(f"SELECT to_regclass('{table}')") is None
        assert await scalar("SELECT to_regclass('candles')") is not None

        await alembic(command.upgrade, alembic_config, "head")
        assert await scalar(triggers) == 14
        assert await scalar("SELECT to_regclass('uq_orders_run_client_order_id')") is not None
        await alembic(command.check, alembic_config)
    finally:
        await engine.dispose()
