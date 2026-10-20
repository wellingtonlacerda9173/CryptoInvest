"""Integridade do diario: replay idempotente, regressao de estado, ids por run, imutabilidade e atomicidade.

Escritos sem acesso a um banco (Docker fora do ar): ainda nao foram executados contra o Postgres/Timescale.
"""

from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from engine.domain.journal import OCO_SUFFIX, OrderRecord
from engine.domain.order import OrderStatus
from engine.infrastructure.persistence.decision_repository import DecisionConflictError
from engine.infrastructure.persistence.order_repository import FillConflictError, OrderConflictError
from engine.infrastructure.persistence.serialization import PersistenceError
from engine.infrastructure.persistence.session import create_session_factory
from engine.infrastructure.persistence.trade_repository import TradeConflictError
from engine.infrastructure.persistence.unit_of_work import UnitOfWork
from tests.integration.journal_helpers import (
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


async def count(engine: AsyncEngine, table: str, run_id: str) -> int:
    async with engine.connect() as conn:
        return (
            await conn.execute(text(f"SELECT count(*) FROM {table} WHERE run_id = :r"), {"r": run_id})
        ).scalar_one()


def entry_kwargs(buy_id: str = "buy-1") -> dict[str, Any]:
    return {
        "order": order(buy_id, status=OrderStatus.FILLED),
        "fill": fill(f"f-{buy_id}", buy_id),
        "position": position(),
        "risk_state": busy_risk_state(),
    }


def exit_kwargs(sell_id: str = "sell-1", trade_id: str = "x1", **extra: Any) -> dict[str, Any]:
    loss = trade(trade_id)
    return {
        "fill": fill(f"f-{sell_id}", sell_id, side="sell", price="90", at=loss.closed_at),
        "trade": loss,
        "position": None,
        "risk_state": busy_risk_state(),
        "order": order(sell_id, side="sell", status=OrderStatus.FILLED),
        **extra,
    }


async def test_replayed_entry_after_the_position_was_closed_does_not_resurrect_it(
    sessions: Sessions, engine: AsyncEngine, run_id: str
) -> None:
    async with UnitOfWork(sessions) as uow:
        assert await uow.record_entry(run_id, **entry_kwargs()) is True
    async with UnitOfWork(sessions) as uow:
        assert await uow.record_exit(run_id, **exit_kwargs()) == 2

    async with UnitOfWork(sessions) as uow:
        assert await uow.record_entry(run_id, **entry_kwargs()) is False
        assert await uow.positions.list_open(run_id) == []

    assert await count(engine, "positions", run_id) == 1
    assert await count(engine, "risk_state", run_id) == 2


async def test_replayed_entry_while_open_keeps_the_newer_position_state(
    sessions: Sessions, run_id: str
) -> None:
    moved = position(stop=D(100), highest_price=D(104), breakeven_reached=True)
    async with UnitOfWork(sessions) as uow:
        await uow.record_entry(run_id, **entry_kwargs())
        await uow.positions.save_open(run_id, moved)

    async with UnitOfWork(sessions) as uow:
        assert await uow.record_entry(run_id, **entry_kwargs()) is False
        assert await uow.positions.list_open(run_id) == [moved]


@pytest.mark.parametrize(
    "regression",
    [
        {"quantity": D(2)},
        {"partial_done": False},
        {"breakeven_reached": False},
        {"highest_price": D(103)},
        {"realized_pnl": D(0)},
    ],
)
async def test_old_position_state_with_the_same_stop_cannot_overwrite_newer_state(
    sessions: Sessions, run_id: str, regression: dict[str, object]
) -> None:
    newer = position(
        stop=D(100),
        quantity=D(1),
        partial_done=True,
        breakeven_reached=True,
        highest_price=D(104),
        realized_pnl=D("5.5"),
        take_profit=None,
    )
    async with UnitOfWork(sessions) as uow:
        await uow.positions.save_open(run_id, newer)

    with pytest.raises(PersistenceError):
        async with UnitOfWork(sessions) as uow:
            await uow.positions.save_open(run_id, newer.model_copy(update=regression))

    async with UnitOfWork(sessions) as uow:
        assert await uow.positions.list_open(run_id) == [newer]


async def test_orders_advance_by_status_rank_and_terminal_states_are_final(
    sessions: Sessions, run_id: str
) -> None:
    coid = "ranked-1"
    new = order(coid, at=T0)
    partial = order(coid, status=OrderStatus.PARTIALLY_FILLED, at=T0 + timedelta(minutes=1)).model_copy(
        update={"filled_quantity": D(1), "average_price": D(100)}
    )
    filled = order(coid, status=OrderStatus.FILLED, at=T0 + timedelta(minutes=2)).model_copy(
        update={"filled_quantity": D(2), "average_price": D(100)}
    )
    async with UnitOfWork(sessions) as uow:
        await uow.orders.upsert(run_id, new)
        await uow.orders.upsert(run_id, partial)
        assert (await uow.orders.get(run_id, coid)) == partial
        await uow.orders.upsert(run_id, filled)
        # replays antigos, mesmo com relogio mais novo, nao regridem o status
        await uow.orders.upsert(run_id, new.model_copy(update={"updated_at": T0 + timedelta(hours=1)}))
        await uow.orders.upsert(run_id, partial)
        assert (await uow.orders.get(run_id, coid)) == filled

    with pytest.raises(OrderConflictError, match="terminal"):
        async with UnitOfWork(sessions) as uow:
            await uow.orders.upsert(
                run_id,
                filled.model_copy(
                    update={"status": OrderStatus.CANCELED, "updated_at": T0 + timedelta(minutes=3)}
                ),
            )


async def test_order_filled_quantity_never_decreases(sessions: Sessions, run_id: str) -> None:
    coid = "ranked-2"
    partial = order(coid, status=OrderStatus.PARTIALLY_FILLED, at=T0).model_copy(
        update={"filled_quantity": D("1.5")}
    )
    async with UnitOfWork(sessions) as uow:
        await uow.orders.upsert(run_id, partial)

    with pytest.raises(OrderConflictError, match="filled_quantity"):
        async with UnitOfWork(sessions) as uow:
            await uow.orders.upsert(
                run_id,
                partial.model_copy(update={"filled_quantity": D(1), "updated_at": T0 + timedelta(minutes=1)}),
            )


async def test_same_client_order_id_in_two_runs_are_independent_orders(
    sessions: Sessions, run_id: str
) -> None:
    other = new_run()
    async with UnitOfWork(sessions) as uow:
        await uow.runs.start(other)
        await uow.orders.upsert(run_id, order("shared-id"))
        await uow.orders.upsert(other.run_id, order("shared-id", status=OrderStatus.FILLED))
        assert (await uow.orders.get(run_id, "shared-id")) == order("shared-id")
        assert (await uow.orders.get(other.run_id, "shared-id")) == order(
            "shared-id", status=OrderStatus.FILLED
        )
        assert [o.client_order_id for o in await uow.orders.list_open(run_id)] == ["shared-id"]
        assert await uow.orders.list_open(other.run_id) == []


async def test_fill_cannot_attach_to_an_order_of_another_run(sessions: Sessions, run_id: str) -> None:
    other = new_run()
    async with UnitOfWork(sessions) as uow:
        await uow.runs.start(other)
        await uow.orders.upsert(run_id, order("only-in-first-run"))

    with pytest.raises(IntegrityError):
        async with UnitOfWork(sessions) as uow:
            await uow.fills.record(other.run_id, fill("f-x", "only-in-first-run"))


async def test_fill_trade_and_decision_ids_are_scoped_by_run(sessions: Sessions, run_id: str) -> None:
    other = new_run()
    async with UnitOfWork(sessions) as uow:
        await uow.runs.start(other)
        for rid in (run_id, other.run_id):
            await uow.orders.upsert(rid, order("buy-1", status=OrderStatus.FILLED))
            assert await uow.fills.record(rid, fill("same-fill", "buy-1"))
            assert await uow.trades.record(rid, trade("same-trade"))
            assert await uow.decisions.record(rid, decision())
        assert decision().decision_key(run_id) != decision().decision_key(other.run_id)
        assert len(await uow.fills.list_for_run(other.run_id)) == 1


async def test_fill_with_same_id_but_different_content_is_a_conflict(sessions: Sessions, run_id: str) -> None:
    async with UnitOfWork(sessions) as uow:
        await uow.orders.upsert(run_id, order("buy-1"))
        assert await uow.fills.record(run_id, fill("f1", "buy-1"))
        assert not await uow.fills.record(run_id, fill("f1", "buy-1", at=T0))

    for changed in (fill("f1", "buy-1", price="101"), fill("f1", "buy-1", side="sell")):
        with pytest.raises(FillConflictError):
            async with UnitOfWork(sessions) as uow:
                await uow.fills.record(run_id, changed)


async def test_decision_with_same_key_but_different_content_is_a_conflict(
    sessions: Sessions, run_id: str
) -> None:
    original = decision()
    async with UnitOfWork(sessions) as uow:
        assert await uow.decisions.record(run_id, original)
        assert not await uow.decisions.record(run_id, original)

    for changed in (
        original.model_copy(update={"reason": "outro motivo"}),
        original.model_copy(update={"context": {"regime": "lateral"}}),
        original.model_copy(update={"confidence": 0.1}),
    ):
        with pytest.raises(DecisionConflictError):
            async with UnitOfWork(sessions) as uow:
                await uow.decisions.record(run_id, changed)


async def test_trade_replay_compares_strategy_risk_and_opening(sessions: Sessions, run_id: str) -> None:
    original = trade("t-cmp")
    async with UnitOfWork(sessions) as uow:
        assert await uow.trades.record(run_id, original, strategy="elephant", risk_amount=D(10))
        assert not await uow.trades.record(run_id, original, strategy="elephant", risk_amount=D(10))

    divergent = [
        {"trade": original, "strategy": "outra", "risk_amount": D(10)},
        {"trade": original, "strategy": "elephant", "risk_amount": D(11)},
        {"trade": original, "strategy": "elephant", "risk_amount": None},
        {
            "trade": original.model_copy(update={"opened_at": T0 - timedelta(hours=1)}),
            "strategy": "elephant",
            "risk_amount": D(10),
        },
    ]
    for kwargs in divergent:
        with pytest.raises(TradeConflictError):
            async with UnitOfWork(sessions) as uow:
                await uow.trades.record(run_id, **kwargs)


async def test_oco_order_with_forty_characters_is_registered_and_its_fill_is_linked(
    sessions: Sessions, engine: AsyncEngine, run_id: str
) -> None:
    entry_id = "e" * 36
    entry = order(entry_id, status=OrderStatus.FILLED)
    loss = trade("t-oco")
    oco = OrderRecord.oco_of(
        entry, status=OrderStatus.FILLED, filled_quantity=D(2), average_price=D(90), at=loss.closed_at
    )
    assert oco.client_order_id == f"{entry_id}{OCO_SUFFIX}"
    assert len(oco.client_order_id) == 40

    async with UnitOfWork(sessions) as uow:
        await uow.record_entry(run_id, order=entry, fill=fill("f-entry", entry_id), position=position())
        await uow.record_exit(
            run_id,
            fill=fill("f-oco", oco.client_order_id, side="sell", price="90", at=loss.closed_at),
            trade=loss,
            position=None,
            risk_state=busy_risk_state(),
            order=oco,
        )
        assert await uow.orders.get(run_id, oco.client_order_id) == oco
        assert [f.client_order_id for f in await uow.fills.list_for_run(run_id)] == [
            entry_id,
            oco.client_order_id,
        ]
    assert await count(engine, "trades", run_id) == 1


async def test_exit_fill_of_an_unregistered_oco_is_refused_and_rolled_back(
    sessions: Sessions, engine: AsyncEngine, run_id: str
) -> None:
    async with UnitOfWork(sessions) as uow:
        await uow.record_entry(run_id, **entry_kwargs())

    kwargs = exit_kwargs()
    kwargs["fill"] = kwargs["fill"].model_copy(update={"client_order_id": f"buy-1{OCO_SUFFIX}"})
    kwargs["order"] = None
    with pytest.raises(PersistenceError, match="nao esta registrada"):
        async with UnitOfWork(sessions) as uow:
            await uow.record_exit(run_id, **kwargs)

    assert await count(engine, "trades", run_id) == 0


async def test_gateway_state_is_saved_atomically_with_entry_and_exit(
    sessions: Sessions, engine: AsyncEngine, run_id: str
) -> None:
    async with UnitOfWork(sessions) as uow:
        await uow.record_entry(run_id, **entry_kwargs(), gateway_state={"cash": D("9800.5"), "seq": 1})
        await uow.record_exit(run_id, **exit_kwargs(), gateway_state={"cash": D("9780"), "seq": 2})

    async with UnitOfWork(sessions) as uow:
        latest = await uow.gateway_states.load_latest(run_id)
    assert latest is not None
    assert (latest.version, latest.state["cash"], latest.saved_at) == (2, "9780", trade("x1").closed_at)
    assert await count(engine, "gateway_state", run_id) == 2


async def test_failure_saving_the_gateway_state_rolls_back_the_whole_entry(
    sessions: Sessions, engine: AsyncEngine, run_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(*args: object, **kwargs: object) -> int:
        raise ConnectionError("queda ao salvar o gateway")

    with pytest.raises(ConnectionError):
        async with UnitOfWork(sessions) as uow:
            monkeypatch.setattr(uow.gateway_states, "save", boom)
            await uow.record_entry(run_id, **entry_kwargs(), gateway_state={"cash": D(1)})

    for table in ("orders", "fills", "positions", "risk_state", "gateway_state"):
        assert await count(engine, table, run_id) == 0, table


async def test_failure_saving_the_gateway_state_rolls_back_the_whole_exit(
    sessions: Sessions, engine: AsyncEngine, run_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with UnitOfWork(sessions) as uow:
        await uow.record_entry(run_id, **entry_kwargs())

    async def boom(*args: object, **kwargs: object) -> int:
        raise ConnectionError("queda ao salvar o gateway")

    with pytest.raises(ConnectionError):
        async with UnitOfWork(sessions) as uow:
            monkeypatch.setattr(uow.gateway_states, "save", boom)
            await uow.record_exit(run_id, **exit_kwargs(), gateway_state={"cash": D(1)})

    assert await count(engine, "trades", run_id) == 0
    assert await count(engine, "fills", run_id) == 1
    async with UnitOfWork(sessions) as uow:
        assert len(await uow.positions.list_open(run_id)) == 1


# --- papel restrito da aplicacao (scripts/db_roles.sql) ------------------------------------------------
# A fixture `app_engine` (conftest) cria um banco descartavel, aplica o script e conecta com o papel restrito.

IMMUTABLE_TABLES = (
    "trades",
    "decisions",
    "control_events",
    "fills",
    "risk_state",
    "gateway_state",
    "audit_log",
    "equity_snapshots",
)


async def test_restricted_role_runs_the_normal_flow_through_the_unit_of_work(
    app_engine: AsyncEngine, scratch_engine: AsyncEngine
) -> None:
    sessions = create_session_factory(app_engine)
    run = new_run()
    async with UnitOfWork(sessions) as uow:
        await uow.runs.start(run)
        await uow.record_entry(run.run_id, **entry_kwargs())
        await uow.record_exit(run.run_id, **exit_kwargs(), gateway_state={"cash": D(1)})
        await uow.runs.finish(run.run_id, T0 + timedelta(days=1))
        await uow.audit("app", "role-check", "probe")
        await uow.decisions.record(run.run_id, decision())
    assert await count(scratch_engine, "trades", run.run_id) == 1


@pytest.mark.parametrize("table", IMMUTABLE_TABLES)
@pytest.mark.parametrize("verb", ["UPDATE {t} SET run_id = run_id", "DELETE FROM {t}", "TRUNCATE {t}"])
async def test_restricted_role_cannot_change_or_delete_immutable_tables(
    app_engine: AsyncEngine, verb: str, table: str
) -> None:
    if table == "audit_log" and verb.startswith("UPDATE"):
        verb = "UPDATE {t} SET action = action"
    with pytest.raises(DBAPIError, match="permission denied"):
        async with app_engine.begin() as conn:
            await conn.execute(text(verb.format(t=table)))


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE runs SET config_hash = 'x'",
        "UPDATE orders SET quantity = 1",
        "UPDATE positions SET entry_price = 1",
        "ALTER TABLE trades DISABLE TRIGGER trades_append_only",
        "DROP TRIGGER trades_append_only ON trades",
        "CREATE TABLE intruso (id int)",
        "DROP TABLE trades",
        "ALTER TABLE orders ADD COLUMN x int",
    ],
)
async def test_restricted_role_cannot_alter_fixed_columns_or_the_schema(
    app_engine: AsyncEngine, statement: str
) -> None:
    with pytest.raises(DBAPIError, match=r"permission denied|must be owner"):
        async with app_engine.begin() as conn:
            await conn.execute(text(statement))


@pytest.mark.parametrize(
    "statement",
    [
        "ALTER TABLE trades DISABLE TRIGGER trades_append_only",
        "ALTER TABLE trades DISABLE TRIGGER trades_no_truncate",
        "ALTER TABLE decisions DISABLE TRIGGER decisions_append_only",
        "ALTER TABLE trades ENABLE REPLICA TRIGGER trades_append_only",
        "ALTER TABLE fills DISABLE TRIGGER USER",
        "ALTER TABLE equity_snapshots DISABLE TRIGGER equity_snapshots_append_only",
        "ALTER TABLE equity_snapshots ENABLE REPLICA TRIGGER equity_snapshots_no_truncate",
    ],
)
async def test_event_trigger_blocks_disabling_or_demoting_append_only_triggers(
    scratch_engine: AsyncEngine, role_setup: object, statement: str
) -> None:
    with pytest.raises(DBAPIError, match="nao podem ser desabilitados"):
        async with scratch_engine.begin() as conn:
            await conn.execute(text(statement))


@pytest.mark.parametrize(
    "statement",
    [
        "DROP TRIGGER trades_append_only ON trades",
        "DROP TRIGGER trades_no_truncate ON trades",
        "DROP TRIGGER equity_snapshots_append_only ON equity_snapshots",
        "DROP FUNCTION forbid_modification() CASCADE",
        "DROP TABLE trades CASCADE",
    ],
)
async def test_event_trigger_blocks_dropping_append_only_triggers_and_function(
    scratch_engine: AsyncEngine, role_setup: object, statement: str
) -> None:
    with pytest.raises(DBAPIError, match="nao podem ser removidos"):
        async with scratch_engine.begin() as conn:
            await conn.execute(text(statement))
    async with scratch_engine.connect() as conn:
        assert await conn.scalar(text("SELECT to_regproc('forbid_modification')")) is not None
        assert await conn.scalar(text("SELECT to_regclass('trades')")) is not None


async def test_event_trigger_still_allows_harmless_ddl_and_re_enabling(
    scratch_engine: AsyncEngine, role_setup: object
) -> None:
    async with scratch_engine.begin() as conn:
        await conn.execute(text("ALTER TABLE trades ENABLE TRIGGER trades_append_only"))
        await conn.execute(text("ALTER TABLE trades ENABLE ALWAYS TRIGGER trades_append_only"))
        await conn.execute(text("ALTER TABLE trades ENABLE TRIGGER trades_append_only"))
        await conn.execute(text("CREATE TABLE scratch_probe (id int)"))
        await conn.execute(text("DROP TABLE scratch_probe"))
