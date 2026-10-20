"""Regras puras do diario (sem banco): transicoes de posicao e ordem, ids e atomicidade do UnitOfWork."""

from datetime import timedelta
from decimal import Decimal
from typing import Any, cast

import pytest

from engine.application.risk import ManagedPosition
from engine.domain.journal import OCO_SUFFIX, OrderRecord
from engine.domain.order import OrderStatus
from engine.infrastructure.persistence.order_repository import OrderConflictError, next_order_state
from engine.infrastructure.persistence.position_repository import check_position_transition
from engine.infrastructure.persistence.serialization import PersistenceError
from engine.infrastructure.persistence.unit_of_work import UnitOfWork
from tests.integration.journal_helpers import T0, busy_risk_state, decision, fill, order, position, trade

D = Decimal


def moved(**overrides: object) -> ManagedPosition:
    values: dict[str, object] = {
        "stop": D(100),
        "highest_price": D(104),
        "breakeven_reached": True,
        "partial_done": True,
        "quantity": D(1),
        "realized_pnl": D("5.5"),
    }
    return position(**(values | overrides))


class TestPositionTransitions:
    def test_forward_progress_is_accepted_and_replay_of_same_state_is_idempotent(self) -> None:
        check_position_transition(position(), moved())
        check_position_transition(moved(), moved())
        check_position_transition(position(), position(stop=D(97), highest_price=D(101)))

    @pytest.mark.parametrize(
        ("regression", "message"),
        [
            ({"quantity": D(2)}, "quantity"),
            ({"partial_done": False}, "partial_done"),
            ({"breakeven_reached": False}, "breakeven_reached"),
            ({"highest_price": D(103)}, "highest_price"),
            ({"realized_pnl": D(0)}, "realized_pnl"),
            ({"take_profit": D(120)}, "take_profit"),
        ],
    )
    def test_old_replay_with_the_same_stop_cannot_overwrite_newer_state(
        self, regression: dict[str, object], message: str
    ) -> None:
        stored = moved(take_profit=None)

        with pytest.raises(PersistenceError, match=message):
            check_position_transition(stored, moved(take_profit=None).model_copy(update=regression))

    def test_stop_never_goes_down(self) -> None:
        with pytest.raises(PersistenceError, match="stop"):
            check_position_transition(moved(), moved(stop=D(97)))

    def test_partial_exit_may_realize_a_net_loss_but_only_with_a_smaller_quantity(self) -> None:
        stored = position()

        check_position_transition(stored, position(quantity=D(1), realized_pnl=D("-0.1"), partial_done=True))
        with pytest.raises(PersistenceError, match="realized_pnl"):
            check_position_transition(stored, position(realized_pnl=D("-0.1")))

    def test_fixed_fields_cannot_change(self) -> None:
        with pytest.raises(PersistenceError, match="entry_price"):
            check_position_transition(position(), position(entry_price=D(101), highest_price=D(101)))
        with pytest.raises(PersistenceError, match="opened_at"):
            check_position_transition(position(), position(opened_at=T0 + timedelta(hours=1)))


def at(minutes: int) -> Any:
    return T0 + timedelta(minutes=minutes)


def progress(status: OrderStatus, minutes: int, filled: str = "0") -> OrderRecord:
    return order("c1", status=status, at=at(minutes)).model_copy(update={"filled_quantity": D(filled)})


class TestOrderRanking:
    def test_order_advances_by_status_rank(self) -> None:
        new, partial, filled = (
            progress(OrderStatus.NEW, 0),
            progress(OrderStatus.PARTIALLY_FILLED, 1, "1"),
            progress(OrderStatus.FILLED, 2, "2"),
        )

        assert next_order_state(new, partial) == partial
        assert next_order_state(partial, filled) == filled
        assert next_order_state(new, filled) == filled

    def test_stale_replays_are_ignored_even_with_a_newer_timestamp(self) -> None:
        partial = progress(OrderStatus.PARTIALLY_FILLED, 1, "1")

        assert next_order_state(partial, progress(OrderStatus.NEW, 5)) is None
        assert next_order_state(progress(OrderStatus.FILLED, 2, "2"), progress(OrderStatus.NEW, 9)) is None
        assert next_order_state(partial, progress(OrderStatus.PARTIALLY_FILLED, 0, "1")) is None

    def test_more_filled_quantity_wins_over_a_newer_timestamp_in_the_same_rank(self) -> None:
        stored = progress(OrderStatus.PARTIALLY_FILLED, 10, "1")
        late = progress(OrderStatus.PARTIALLY_FILLED, 5, "1.5")

        result = next_order_state(stored, late)

        assert result is not None
        assert result.filled_quantity == D("1.5")
        assert result.updated_at == stored.updated_at  # o relogio nunca regride
        assert next_order_state(stored, progress(OrderStatus.PARTIALLY_FILLED, 5, "1")) is None

    def test_a_new_exchange_order_id_advances_even_with_an_older_timestamp(self) -> None:
        stored = progress(OrderStatus.NEW, 10)
        acked = progress(OrderStatus.NEW, 5).model_copy(update={"exchange_order_id": "123"})

        result = next_order_state(stored, acked)

        assert result is not None
        assert result.exchange_order_id == "123"

    def test_filled_quantity_never_decreases(self) -> None:
        partial = progress(OrderStatus.PARTIALLY_FILLED, 1, "1.5")

        with pytest.raises(OrderConflictError, match="filled_quantity"):
            next_order_state(partial, progress(OrderStatus.PARTIALLY_FILLED, 2, "1"))
        assert next_order_state(partial, progress(OrderStatus.PARTIALLY_FILLED, 2, "1.5")) is not None

    def test_terminal_status_is_final(self) -> None:
        filled = progress(OrderStatus.FILLED, 2, "2")

        assert next_order_state(filled, filled) is None
        with pytest.raises(OrderConflictError, match="terminal"):
            next_order_state(filled, progress(OrderStatus.CANCELED, 3, "2"))
        with pytest.raises(OrderConflictError, match="filled_quantity"):
            next_order_state(filled, progress(OrderStatus.FILLED, 3, "1"))

    def test_canceling_a_partially_filled_order_is_allowed(self) -> None:
        canceled = progress(OrderStatus.CANCELED, 3, "1")

        assert next_order_state(progress(OrderStatus.PARTIALLY_FILLED, 1, "1"), canceled) == canceled

    def test_fixed_fields_cannot_change(self) -> None:
        stored = progress(OrderStatus.NEW, 0)

        with pytest.raises(OrderConflictError, match="quantity"):
            next_order_state(stored, stored.model_copy(update={"quantity": D(3)}))


class TestJournalModels:
    def test_decision_key_is_scoped_by_run(self) -> None:
        entry = decision()

        assert entry.decision_key("run-a") == entry.decision_key("run-a")
        assert entry.decision_key("run-a") != entry.decision_key("run-b")
        assert len(entry.decision_key("run-a")) == 48

    def test_oco_order_id_with_forty_characters_is_valid_and_linked_to_the_entry(self) -> None:
        entry_id = "e" * 36
        entry = order(entry_id, status=OrderStatus.FILLED)

        oco = OrderRecord.oco_of(entry, status=OrderStatus.FILLED, filled_quantity=D(2), at=at(30))

        assert oco.client_order_id == f"{entry_id}{OCO_SUFFIX}"
        assert len(oco.client_order_id) == 40
        assert (oco.side, oco.type, oco.stop_loss, oco.take_profit) == ("sell", "oco", D(95), D(110))
        assert fill("f-oco", oco.client_order_id, side="sell").client_order_id == oco.client_order_id


class FakeTransaction:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def commit(self) -> None:
        self.events.append("commit")

    async def rollback(self) -> None:
        self.events.append("rollback")


class FakeSession:
    def __init__(self, *, begin_error: Exception | None = None) -> None:
        self.transaction = FakeTransaction()
        self.closed = False
        self._begin_error = begin_error

    async def begin(self) -> FakeTransaction:
        if self._begin_error:
            raise self._begin_error
        return self.transaction

    async def close(self) -> None:
        self.closed = True


class Recorder:
    """Repositorio falso: registra as chamadas na ordem global e responde conforme configurado."""

    def __init__(self, name: str, calls: list[str], **results: object) -> None:
        self._name, self._calls, self._results = name, calls, results

    def __getattr__(self, method: str) -> Any:
        async def call(*args: object, **kwargs: object) -> object:
            self._calls.append(f"{self._name}.{method}")
            return self._results.get(method)

        return call


async def enter_with_fakes(
    session: FakeSession, calls: list[str], **results: dict[str, object]
) -> UnitOfWork:
    uow = UnitOfWork(cast("Any", lambda: session))
    await uow.__aenter__()
    for name in ("orders", "fills", "positions", "trades", "risk_states", "gateway_states"):
        setattr(uow, name, Recorder(name, calls, **results.get(name, {})))
    return uow


class TestUnitOfWorkAtomicity:
    async def test_replayed_entry_does_not_touch_position_risk_or_gateway(self) -> None:
        calls: list[str] = []
        session = FakeSession()
        uow = await enter_with_fakes(session, calls, fills={"record": False})

        applied = await uow.record_entry(
            "r1",
            order=order("buy-1", status=OrderStatus.FILLED),
            fill=fill("f1", "buy-1"),
            position=position(),
            risk_state=busy_risk_state(),
            gateway_state={"cash": D(1)},
        )

        assert applied is False
        assert calls == ["orders.upsert", "fills.record"]

    async def test_first_entry_saves_position_risk_and_gateway_state_together(self) -> None:
        calls: list[str] = []
        uow = await enter_with_fakes(FakeSession(), calls, fills={"record": True})

        applied = await uow.record_entry(
            "r1",
            order=order("buy-1", status=OrderStatus.FILLED),
            fill=fill("f1", "buy-1"),
            position=position(),
            risk_state=busy_risk_state(),
            gateway_state={"cash": D(1)},
        )

        assert applied is True
        assert calls == [
            "orders.upsert",
            "fills.record",
            "positions.save_open",
            "risk_states.save",
            "gateway_states.save",
        ]

    async def test_exit_saves_gateway_state_in_the_same_unit_and_replay_is_a_noop(self) -> None:
        loss = trade("t1")
        sell = fill("f-s", "sell-1", side="sell", price="90", at=loss.closed_at)
        kwargs: dict[str, Any] = {
            "fill": sell,
            "trade": loss,
            "position": None,
            "risk_state": busy_risk_state(),
            "order": order("sell-1", side="sell", status=OrderStatus.FILLED),
            "gateway_state": {"cash": D(1)},
        }
        calls: list[str] = []
        uow = await enter_with_fakes(
            FakeSession(),
            calls,
            trades={"record": True},
            fills={"record": True},
            positions={"close": True},
            risk_states={"save": 7},
        )

        assert await uow.record_exit("r1", **kwargs) == 7
        assert calls == [
            "trades.record",
            "orders.upsert",
            "fills.record",
            "positions.close",
            "risk_states.save",
            "gateway_states.save",
        ]

        replay_calls: list[str] = []
        replay = await enter_with_fakes(FakeSession(), replay_calls, trades={"record": False})
        assert await replay.record_exit("r1", **kwargs) is None
        assert replay_calls == ["trades.record", "orders.upsert", "fills.record"]  # confere, mas nao reaplica

    async def test_exit_requires_the_fill_order_to_exist_oco_included(self) -> None:
        loss = trade("t2")
        oco_id = "e" * 36 + OCO_SUFFIX
        uow = await enter_with_fakes(FakeSession(), [], trades={"record": True}, orders={"get": None})

        with pytest.raises(PersistenceError, match="nao esta registrada"):
            await uow.record_exit(
                "r1",
                fill=fill("f-oco", oco_id, side="sell", price="90", at=loss.closed_at),
                trade=loss,
                position=None,
                risk_state=busy_risk_state(),
            )

    async def test_session_is_closed_when_begin_fails(self) -> None:
        session = FakeSession(begin_error=ConnectionError("sem banco"))
        uow = UnitOfWork(cast("Any", lambda: session))

        with pytest.raises(ConnectionError):
            await uow.__aenter__()

        assert session.closed

    async def test_exit_commits_on_success_and_rolls_back_on_error(self) -> None:
        ok = FakeSession()
        async with UnitOfWork(cast("Any", lambda: ok)):
            pass
        assert (ok.transaction.events, ok.closed) == (["commit"], True)

        bad = FakeSession()
        with pytest.raises(RuntimeError):
            async with UnitOfWork(cast("Any", lambda: bad)):
                raise RuntimeError
        assert (bad.transaction.events, bad.closed) == (["rollback"], True)
