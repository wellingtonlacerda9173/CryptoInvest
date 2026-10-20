"""Reverificacao QA da persistencia da Fase 3: tenta quebrar replays, conflitos, atomicidade e papeis.

Testes `BUG:` sao xfail(strict): documentam um defeito comprovado e passam a falhar (XPASS) quando corrigido.
"""

import asyncio
import random
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from engine.application.backtest.models import EquityPoint
from engine.application.risk import ManagedPosition, manage_position
from engine.domain.journal import OrderRecord
from engine.domain.order import OrderStatus
from engine.domain.risk import RiskConfig
from engine.infrastructure.persistence.order_repository import FillConflictError, OrderConflictError
from engine.infrastructure.persistence.position_repository import check_position_transition
from engine.infrastructure.persistence.serialization import PersistenceError
from engine.infrastructure.persistence.session import create_session_factory
from engine.infrastructure.persistence.unit_of_work import UnitOfWork
from tests.integration.journal_helpers import (
    SYMBOL,
    T0,
    fill,
    new_run,
    order,
    position,
    trade,
)
from tests.integration.test_journal_integrity import count, entry_kwargs, exit_kwargs
from tests.unit.risk.factories import NOW, candle, rules

pytestmark = pytest.mark.integration

D = Decimal
Sessions = async_sessionmaker[AsyncSession]


@pytest.fixture
async def run_id(sessions: Sessions) -> str:
    run = new_run()
    async with UnitOfWork(sessions) as uow:
        await uow.runs.start(run)
    return run.run_id


async def test_entry_replay_does_not_save_the_gateway_state_or_risk_state_again(
    sessions: Sessions, engine: AsyncEngine, run_id: str
) -> None:
    async with UnitOfWork(sessions) as uow:
        assert await uow.record_entry(run_id, **entry_kwargs(), gateway_state={"v": 1}) is True
    async with UnitOfWork(sessions) as uow:
        assert await uow.record_entry(run_id, **entry_kwargs(), gateway_state={"v": 2}) is False

    assert await count(engine, "gateway_state", run_id) == 1
    assert await count(engine, "risk_state", run_id) == 1


async def test_exit_replay_is_a_noop_for_every_table(
    sessions: Sessions, engine: AsyncEngine, run_id: str
) -> None:
    async with UnitOfWork(sessions) as uow:
        await uow.record_entry(run_id, **entry_kwargs())
    async with UnitOfWork(sessions) as uow:
        assert await uow.record_exit(run_id, **exit_kwargs(), gateway_state={"v": 1}) == 2
    before = {
        t: await count(engine, t, run_id)
        for t in ("orders", "fills", "trades", "risk_state", "gateway_state")
    }

    for _ in range(3):
        async with UnitOfWork(sessions) as uow:
            assert await uow.record_exit(run_id, **exit_kwargs(), gateway_state={"v": 9}) is None

    after = {t: await count(engine, t, run_id) for t in before}
    assert after == before


async def test_replayed_exit_with_a_different_fill_price_is_not_silently_accepted(
    sessions: Sessions, run_id: str
) -> None:
    async with UnitOfWork(sessions) as uow:
        await uow.record_entry(run_id, **entry_kwargs())
    async with UnitOfWork(sessions) as uow:
        await uow.record_exit(run_id, **exit_kwargs())

    tampered = exit_kwargs()
    tampered["fill"] = fill("f-sell-1", "sell-1", side="sell", price="12345", at=tampered["trade"].closed_at)
    with pytest.raises(FillConflictError):
        async with UnitOfWork(sessions) as uow:
            await uow.record_exit(run_id, **tampered)


async def test_partial_exit_then_final_exit_with_replays_in_between(
    sessions: Sessions, engine: AsyncEngine, run_id: str
) -> None:
    async with UnitOfWork(sessions) as uow:
        await uow.record_entry(run_id, **entry_kwargs())
    partial_position = position(
        quantity=D(1), partial_done=True, stop=D(100), take_profit=None, realized_pnl=D("9.8")
    )
    partial = exit_kwargs("sell-p", "tp-partial", position=partial_position)
    partial["trade"] = trade("tp-partial", exit_price="110", closed=T0 + timedelta(hours=2))
    partial["fill"] = fill("f-sell-p", "sell-p", side="sell", price="110", at=T0 + timedelta(hours=2))

    async with UnitOfWork(sessions) as uow:
        assert await uow.record_exit(run_id, **partial) == 2
    async with UnitOfWork(sessions) as uow:
        assert await uow.record_exit(run_id, **partial) is None
        assert await uow.positions.list_open(run_id) == [partial_position]
    async with UnitOfWork(sessions) as uow:
        assert await uow.record_exit(run_id, **exit_kwargs("sell-f", "final")) == 3
    async with UnitOfWork(sessions) as uow:
        assert await uow.record_exit(run_id, **partial) is None
        assert await uow.positions.list_open(run_id) == []

    assert await count(engine, "trades", run_id) == 2


async def test_failed_exit_with_position_regression_rolls_back_trade_fill_and_order(
    sessions: Sessions, engine: AsyncEngine, run_id: str
) -> None:
    newer = position(stop=D(100), highest_price=D(104), breakeven_reached=True)
    async with UnitOfWork(sessions) as uow:
        await uow.record_entry(run_id, **entry_kwargs())
        await uow.positions.save_open(run_id, newer)
    snapshot = {t: await count(engine, t, run_id) for t in ("orders", "fills", "trades", "risk_state")}

    regressed = exit_kwargs("sell-9", "x9", position=position(stop=D(97), quantity=D(1)))
    with pytest.raises(PersistenceError):
        async with UnitOfWork(sessions) as uow:
            await uow.record_exit(run_id, **regressed)

    assert {t: await count(engine, t, run_id) for t in snapshot} == snapshot


async def test_concurrent_identical_entries_and_exits_apply_exactly_once(
    sessions: Sessions, engine: AsyncEngine, run_id: str
) -> None:
    async def entry() -> bool:
        async with UnitOfWork(sessions) as uow:
            return await uow.record_entry(run_id, **entry_kwargs())

    results = await asyncio.gather(*(entry() for _ in range(8)))
    assert results.count(True) == 1

    async def leave() -> int | None:
        async with UnitOfWork(sessions) as uow:
            return await uow.record_exit(run_id, **exit_kwargs())

    exits = await asyncio.gather(*(leave() for _ in range(8)))
    assert [e for e in exits if e is not None] == [2]
    assert await count(engine, "trades", run_id) == 1
    assert await count(engine, "fills", run_id) == 2
    assert await count(engine, "risk_state", run_id) == 2


async def test_oco_with_the_longest_allowed_entry_id_is_registered_filled_and_replayed(
    sessions: Sessions, engine: AsyncEngine, run_id: str
) -> None:
    entry_id = "e" * 36
    entry = order(entry_id, status=OrderStatus.FILLED)
    oco_new = OrderRecord.oco_of(entry)
    assert len(oco_new.client_order_id) == 40
    async with UnitOfWork(sessions) as uow:
        await uow.record_entry(run_id, **{**entry_kwargs(entry_id), "order": entry})
        await uow.orders.upsert(run_id, oco_new)
    oco_done = OrderRecord.oco_of(
        entry,
        status=OrderStatus.FILLED,
        filled_quantity=D(2),
        average_price=D(90),
        at=T0 + timedelta(hours=3),
    )
    kwargs = exit_kwargs(f"{entry_id}:oco", "oco-trade", order=oco_done)
    async with UnitOfWork(sessions) as uow:
        assert await uow.record_exit(run_id, **kwargs) == 2
    async with UnitOfWork(sessions) as uow:
        assert await uow.record_exit(run_id, **kwargs) is None
        stored = await uow.orders.get(run_id, f"{entry_id}:oco")
        assert await uow.orders.list_open(run_id) == []
    assert stored is not None
    assert stored.status is OrderStatus.FILLED
    assert await count(engine, "orders", run_id) == 2


async def test_a_run_never_sees_the_orders_of_another_run(sessions: Sessions) -> None:
    other = new_run()
    mine = new_run()
    async with UnitOfWork(sessions) as uow:
        await uow.runs.start(other)
        await uow.runs.start(mine)
        await uow.record_entry(other.run_id, **entry_kwargs("same-id"))
    async with UnitOfWork(sessions) as uow:
        assert await uow.orders.get(mine.run_id, "same-id") is None
        assert await uow.record_entry(mine.run_id, **entry_kwargs("same-id")) is True
        assert len(await uow.positions.list_open(mine.run_id)) == 1


async def test_out_of_order_partial_fill_with_older_timestamp_but_more_filled_quantity_is_kept(
    sessions: Sessions, run_id: str
) -> None:
    def partial(filled: str, at_minutes: int) -> OrderRecord:
        return order(
            "p-1", status=OrderStatus.PARTIALLY_FILLED, at=T0 + timedelta(minutes=at_minutes)
        ).model_copy(update={"filled_quantity": D(filled)})

    async with UnitOfWork(sessions) as uow:
        await uow.orders.upsert(run_id, partial("1", 10))
        await uow.orders.upsert(run_id, partial("1.5", 5))
        stored = await uow.orders.get(run_id, "p-1")
    assert stored is not None
    assert stored.filled_quantity == D("1.5")


async def test_order_conflicts_inside_the_unit_of_work_roll_everything_back(
    sessions: Sessions, engine: AsyncEngine, run_id: str
) -> None:
    async with UnitOfWork(sessions) as uow:
        await uow.record_entry(run_id, **entry_kwargs())
    tampered = entry_kwargs("buy-1")
    tampered["order"] = tampered["order"].model_copy(update={"quantity": D(3)})
    with pytest.raises(OrderConflictError):
        async with UnitOfWork(sessions) as uow:
            await uow.audit("qa", "x", "y")
            await uow.record_entry(run_id, **tampered)
    async with engine.connect() as conn:
        n = await conn.scalar(text("SELECT count(*) FROM audit_log WHERE actor = 'qa' AND action = 'x'"))
    assert n == 0


async def test_every_state_manage_position_can_produce_is_accepted_by_the_position_guard() -> None:
    rng = random.Random(7)  # noqa: S311
    rule = rules()
    for _ in range(400):
        cfg = RiskConfig(
            profit_giveback=D(rng.choice(["0.2", "0.4", "0.9"])), taker_fee_rate=D(rng.choice(["0", "0.001"]))
        )
        size = D(rng.choice(["0.003", "1", "7.123"]))
        pos = ManagedPosition(
            symbol=SYMBOL,
            entry_price=D(100),
            initial_quantity=size,
            quantity=size,
            initial_stop=D(95),
            stop=D(95),
            take_profit=D(110),
            highest_price=D(100),
            entry_fee=D("0.2"),
            opened_at=NOW - timedelta(hours=1),
        )
        for i in range(40):
            o, h, low, c = (D(rng.randint(60, 160)) / 2 for _ in range(4))
            bar = candle(
                str(o), str(max(o, h, low, c)), str(min(o, h, low, c)), str(c), NOW + timedelta(hours=i)
            )
            result = manage_position(pos, bar, rule, cfg)
            if result.position is None:
                break
            check_position_transition(pos, result.position)
            pos = result.position


async def test_gateway_state_survives_the_jsonb_roundtrip_exactly(sessions: Sessions, run_id: str) -> None:
    from engine.infrastructure.paper import PaperGateway
    from engine.infrastructure.paper.state import PaperState
    from tests.unit.paper.helpers import FakeMarket, book, opened

    gateway, clock = await opened(
        stop="90.123456789012", target="120.5", qty="0.123456789012345678", slippage_bps=D("5.5")
    )
    clock.advance(seconds=1)
    gateway.on_book(book("89", "89.5"))
    snapshot = gateway.snapshot()
    assert snapshot.fills
    async with UnitOfWork(sessions) as uow:
        await uow.gateway_states.save(run_id, snapshot.model_dump(mode="json"), saved_at=T0)
    async with UnitOfWork(sessions) as uow:
        loaded = await uow.gateway_states.load_latest(run_id)
    assert loaded is not None
    restored_state = PaperState.model_validate(loaded.state)
    assert restored_state == snapshot
    assert PaperGateway.restore(restored_state, FakeMarket(), clock).snapshot() == snapshot


# papel restrito (banco descartavel com scripts/db_roles.sql aplicado; ver conftest)


async def test_restricted_role_writes_equity_but_cannot_touch_hypertable_chunks(
    app_engine: AsyncEngine, scratch_engine: AsyncEngine
) -> None:
    sessions = create_session_factory(app_engine)
    run = new_run()
    async with UnitOfWork(sessions) as uow:
        await uow.runs.start(run)
        await uow.equity.record(run.run_id, EquityPoint(at=T0, equity=D(1), realized=D(0), in_position=False))
    async with scratch_engine.connect() as conn:
        chunk = await conn.scalar(
            text(
                "SELECT format('%I.%I', chunk_schema, chunk_name) FROM timescaledb_information.chunks "
                "WHERE hypertable_name = 'equity_snapshots' LIMIT 1"
            )
        )
    assert chunk
    for statement in (f"UPDATE {chunk} SET equity = 0", f"DELETE FROM {chunk}", f"TRUNCATE {chunk}"):
        with pytest.raises(DBAPIError, match="permission denied"):
            async with app_engine.begin() as conn:
                await conn.execute(text(statement))


async def test_restricted_role_cannot_use_session_replication_role(app_engine: AsyncEngine) -> None:
    with pytest.raises(DBAPIError, match="permission denied"):
        async with app_engine.begin() as conn:
            await conn.execute(text("SET session_replication_role = replica"))


async def test_unique_by_run_blocks_duplicates_inside_a_run_only(sessions: Sessions, run_id: str) -> None:
    async with UnitOfWork(sessions) as uow:
        await uow.record_entry(run_id, **entry_kwargs())
    with pytest.raises(IntegrityError):
        async with UnitOfWork(sessions) as uow:
            await uow._session.execute(
                text(
                    "INSERT INTO fills (fill_id, run_id, client_order_id, symbol, side, quantity, price, "
                    "fee, filled_at) "
                    "VALUES ('f-buy-1', :r, 'buy-1', 'BTC/USDT', 'buy', 1, 1, 0, now())"
                ),
                {"r": run_id},
            )


@pytest.mark.parametrize("field", ["price", "stop_loss", "take_profit", "quantity", "side", "symbol"])
async def test_fixed_order_fields_cannot_be_rewritten_by_a_later_update(
    sessions: Sessions, run_id: str, field: str
) -> None:
    original = order("fx-1", status=OrderStatus.NEW)
    changed = {
        "price": D(101),
        "stop_loss": D(94),
        "take_profit": D(120),
        "quantity": D(3),
        "side": "sell",
        "symbol": "ETH/USDT",
    }[field]
    async with UnitOfWork(sessions) as uow:
        await uow.orders.upsert(run_id, original)
    with pytest.raises(OrderConflictError):
        async with UnitOfWork(sessions) as uow:
            await uow.orders.upsert(
                run_id,
                original.model_copy(
                    update={
                        field: changed,
                        "status": OrderStatus.FILLED,
                        "updated_at": T0 + timedelta(minutes=1),
                    }
                ),
            )
