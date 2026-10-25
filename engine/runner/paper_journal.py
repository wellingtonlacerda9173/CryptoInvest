"""Porta de persistencia do runner e sua implementacao sobre o `UnitOfWork` (uma transacao por evento)."""

import contextlib
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from engine.application.backtest.models import EquityPoint
from engine.application.risk import ManagedPosition, RiskState
from engine.domain.journal import (
    ControlAction,
    ControlEvent,
    DecisionEntry,
    FillRecord,
    OrderRecord,
    RunMode,
    RunRecord,
)
from engine.domain.trade import Trade
from engine.infrastructure.persistence.models import ControlEventRow, DecisionRow, RunRow
from engine.infrastructure.persistence.position_repository import PositionRepository
from engine.infrastructure.persistence.recovery import RecoveryState, load_recovery_state
from engine.infrastructure.persistence.run_config_change import (
    CHANGE_ACTION,
    change_audit,
    current_hash,
    versioned_change_allowed,
)
from engine.infrastructure.persistence.run_repository import EquityConflictError, RunRepository
from engine.infrastructure.persistence.serialization import ConflictError
from engine.infrastructure.persistence.unit_of_work import UnitOfWork


class Journal(Protocol):
    """Tudo que o robo grava. Cada metodo e atomico: estado do gateway e do risco viajam junto do evento."""

    run_id: str

    async def load(self) -> RecoveryState: ...

    async def start_run(self, run: RunRecord, config: Mapping[str, Any], event: ControlEvent) -> None: ...

    async def record_decision(
        self,
        entry: DecisionEntry,
        *,
        order: OrderRecord | None = None,
        gateway_state: Mapping[str, Any] | None = None,
    ) -> bool: ...

    async def has_decision(self, symbol: str, decided_at: datetime) -> bool: ...

    async def update_order(
        self, order: OrderRecord, *, gateway_state: Mapping[str, Any] | None = None
    ) -> None:
        """Linha da ordem e (se informado) o estado do gateway que a reflete, na MESMA transacao."""
        ...

    async def previous_halt(self) -> tuple[str, str] | None:
        """(run, motivo) do ultimo HALT sem REACTIVATE posterior em qualquer run de paper, se houver."""
        ...

    async def record_entry(
        self,
        *,
        orders: Sequence[OrderRecord],
        fill: FillRecord,
        position: ManagedPosition | None,
        risk_state: RiskState,
        gateway_state: Mapping[str, Any],
        decision: DecisionEntry | None = None,
    ) -> bool: ...

    async def record_exit(
        self,
        *,
        orders: Sequence[OrderRecord],
        fill: FillRecord,
        trade: Trade,
        position: ManagedPosition | None,
        risk_state: RiskState,
        strategy: str | None,
        risk_amount: Any,
        gateway_state: Mapping[str, Any],
        without_position_row: bool = False,
        halt: ControlEvent | None = None,
    ) -> None:
        """`halt`: desligamento causado por este trade, na mesma transacao (nunca estado sem o trade)."""
        ...

    async def record_position(self, position: ManagedPosition, gateway_state: Mapping[str, Any]) -> None: ...

    async def save_state(
        self, risk_state: RiskState, gateway_state: Mapping[str, Any], at: datetime
    ) -> None: ...

    async def record_control(
        self,
        event: ControlEvent,
        *,
        risk_state: RiskState | None = None,
        gateway_state: Mapping[str, Any] | None = None,
        audit: Mapping[str, Any] | None = None,
    ) -> None: ...

    async def record_equity(self, point: EquityPoint) -> bool: ...

    async def open_positions(self) -> list[ManagedPosition]: ...


class SqlJournal:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], run_id: str) -> None:
        self._sessions = sessions
        self.run_id = run_id

    async def load(self) -> RecoveryState:
        return await load_recovery_state(self._sessions, self.run_id)

    async def start_run(self, run: RunRecord, config: Mapping[str, Any], event: ControlEvent) -> None:
        stored = await self._stored_run()
        accepted: str | None = None
        changed = False
        if stored is not None and stored.mode == run.mode:
            baseline = await current_hash(self._sessions, stored)
            if run.config_hash == baseline:
                accepted = baseline
            elif await versioned_change_allowed(self._sessions, stored, config):
                accepted, changed = run.config_hash, True
        async with UnitOfWork(self._sessions) as uow:
            await uow.runs.start(run, accepted_hash=accepted)
            await uow.control_events.append(self.run_id, event)
            await uow.audit(
                event.operator,
                "paper_start",
                self.run_id,
                {"config": dict(config), "code_version": run.code_version, "reason": event.reason},
            )
            if changed and stored is not None:
                await uow.audit(event.operator, CHANGE_ACTION, self.run_id, change_audit(stored, run, config))

    async def _stored_run(self) -> RunRecord | None:
        async with self._sessions() as session:
            return await RunRepository(session).get(self.run_id)

    async def record_decision(
        self,
        entry: DecisionEntry,
        *,
        order: OrderRecord | None = None,
        gateway_state: Mapping[str, Any] | None = None,
    ) -> bool:
        async with UnitOfWork(self._sessions) as uow:
            try:
                created = await uow.decisions.record(self.run_id, entry)
            except ConflictError:
                created = False
            if order is not None:
                await uow.orders.upsert(self.run_id, order)
            if gateway_state is not None:
                await uow.gateway_states.save(self.run_id, gateway_state, saved_at=entry.decided_at)
        return created

    async def has_decision(self, symbol: str, decided_at: datetime) -> bool:
        async with self._sessions() as session:
            stmt = (
                select(DecisionRow.id)
                .where(
                    DecisionRow.run_id == self.run_id,
                    DecisionRow.symbol == symbol,
                    DecisionRow.decided_at == decided_at,
                )
                .limit(1)
            )
            return await session.scalar(stmt) is not None

    async def update_order(
        self, order: OrderRecord, *, gateway_state: Mapping[str, Any] | None = None
    ) -> None:
        async with UnitOfWork(self._sessions) as uow:
            await uow.orders.upsert(self.run_id, order)
            if gateway_state is not None:
                await uow.gateway_states.save(self.run_id, gateway_state, saved_at=order.updated_at)

    async def previous_halt(self) -> tuple[str, str] | None:
        # o ultimo HALT/REACTIVATE de QUALQUER run de paper decide: um run intermediario que morreu antes de
        # herdar nao "lava" o desligamento
        async with self._sessions() as session:
            last = (
                await session.execute(
                    select(ControlEventRow.run_id, ControlEventRow.action, ControlEventRow.reason)
                    .join(RunRow, RunRow.run_id == ControlEventRow.run_id)
                    .where(
                        RunRow.mode == RunMode.PAPER.value,
                        ControlEventRow.run_id != self.run_id,
                        ControlEventRow.action.in_(
                            (ControlAction.HALT.value, ControlAction.REACTIVATE.value)
                        ),
                    )
                    .order_by(ControlEventRow.at.desc(), ControlEventRow.id.desc())
                    .limit(1)
                )
            ).first()
        if last is None or last.action != ControlAction.HALT.value:
            return None
        return last.run_id, last.reason

    async def record_entry(
        self,
        *,
        orders: Sequence[OrderRecord],
        fill: FillRecord,
        position: ManagedPosition | None,
        risk_state: RiskState,
        gateway_state: Mapping[str, Any],
        decision: DecisionEntry | None = None,
    ) -> bool:
        async with UnitOfWork(self._sessions) as uow:
            if decision is not None:
                with contextlib.suppress(ConflictError):
                    await uow.decisions.record(self.run_id, decision)
            entry, *others = orders
            if position is not None:
                applied = await uow.record_entry(
                    self.run_id,
                    order=entry,
                    fill=fill,
                    position=position,
                    risk_state=risk_state,
                    gateway_state=gateway_state,
                )
            else:
                await uow.orders.upsert(self.run_id, entry)
                applied = await uow.fills.record(self.run_id, fill)
                if applied:
                    await uow.risk_states.save(self.run_id, risk_state, saved_at=fill.filled_at)
                    await uow.gateway_states.save(self.run_id, gateway_state, saved_at=fill.filled_at)
            if applied:
                for order in others:
                    await uow.orders.upsert(self.run_id, order)
            return applied

    async def record_exit(
        self,
        *,
        orders: Sequence[OrderRecord],
        fill: FillRecord,
        trade: Trade,
        position: ManagedPosition | None,
        risk_state: RiskState,
        strategy: str | None,
        risk_amount: Any,
        gateway_state: Mapping[str, Any],
        without_position_row: bool = False,
        halt: ControlEvent | None = None,
    ) -> None:
        async with UnitOfWork(self._sessions) as uow:
            for order in orders:
                await uow.orders.upsert(self.run_id, order)
            if without_position_row:
                recorded = await uow.trades.record(
                    self.run_id, trade, strategy=strategy, risk_amount=risk_amount
                )
                await uow.fills.record(self.run_id, fill)
                if recorded:
                    await uow.risk_states.save(self.run_id, risk_state, saved_at=trade.closed_at)
                    await uow.gateway_states.save(self.run_id, gateway_state, saved_at=trade.closed_at)
                    if halt is not None:
                        await uow.record_control(self.run_id, halt)
                return
            await uow.record_exit(
                self.run_id,
                fill=fill,
                trade=trade,
                position=position,
                risk_state=risk_state,
                strategy=strategy,
                risk_amount=risk_amount,
                gateway_state=gateway_state,
                control=halt,
            )

    async def record_position(self, position: ManagedPosition, gateway_state: Mapping[str, Any]) -> None:
        async with UnitOfWork(self._sessions) as uow:
            await uow.positions.save_open(self.run_id, position)
            await uow.gateway_states.save(self.run_id, gateway_state, saved_at=position.opened_at)

    async def save_state(self, risk_state: RiskState, gateway_state: Mapping[str, Any], at: datetime) -> None:
        async with UnitOfWork(self._sessions) as uow:
            await uow.risk_states.save(self.run_id, risk_state, saved_at=at)
            await uow.gateway_states.save(self.run_id, gateway_state, saved_at=at)

    async def record_control(
        self,
        event: ControlEvent,
        *,
        risk_state: RiskState | None = None,
        gateway_state: Mapping[str, Any] | None = None,
        audit: Mapping[str, Any] | None = None,
    ) -> None:
        async with UnitOfWork(self._sessions) as uow:
            if risk_state is not None:
                await uow.risk_states.save(self.run_id, risk_state, saved_at=event.at)
            if gateway_state is not None:
                await uow.gateway_states.save(self.run_id, gateway_state, saved_at=event.at)
            await uow.record_control(self.run_id, event, audit)

    async def record_equity(self, point: EquityPoint) -> bool:
        try:
            async with UnitOfWork(self._sessions) as uow:
                return await uow.equity.record(self.run_id, point)
        except EquityConflictError:
            return False

    async def open_positions(self) -> list[ManagedPosition]:
        async with self._sessions() as session:
            return await PositionRepository(session).list_open(self.run_id)
