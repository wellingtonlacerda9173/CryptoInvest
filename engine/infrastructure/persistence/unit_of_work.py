from collections.abc import Mapping
from decimal import Decimal
from types import TracebackType
from typing import Any, Self

from pydantic_core import to_jsonable_python
from sqlalchemy.ext.asyncio import AsyncSession, AsyncSessionTransaction, async_sessionmaker

from engine.application.risk import ManagedPosition, RiskState
from engine.domain.journal import ControlEvent, FillRecord, OrderRecord
from engine.domain.trade import Trade
from engine.infrastructure.persistence.decision_repository import DecisionRepository
from engine.infrastructure.persistence.models import AuditLogRow
from engine.infrastructure.persistence.order_repository import FillRepository, OrderRepository
from engine.infrastructure.persistence.position_repository import PositionRepository
from engine.infrastructure.persistence.run_repository import (
    ControlEventRepository,
    EquitySnapshotRepository,
    RunRepository,
)
from engine.infrastructure.persistence.serialization import PersistenceError
from engine.infrastructure.persistence.state_repository import GatewayStateRepository, RiskStateRepository
from engine.infrastructure.persistence.trade_repository import TradeRepository


class UnitOfWork:
    """Uma transacao para varios repositorios: commit ao sair sem erro, rollback total em qualquer excecao."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions
        self._session: AsyncSession | None = None
        self._transaction: AsyncSessionTransaction | None = None

    async def __aenter__(self) -> Self:
        session = self._session = self._sessions()
        try:
            self._transaction = await session.begin()
        except BaseException:
            self._session = None
            await session.close()
            raise
        self.runs = RunRepository(session)
        self.orders = OrderRepository(session)
        self.fills = FillRepository(session)
        self.positions = PositionRepository(session)
        self.trades = TradeRepository(session)
        self.decisions = DecisionRepository(session)
        self.risk_states = RiskStateRepository(session)
        self.gateway_states = GatewayStateRepository(session)
        self.equity = EquitySnapshotRepository(session)
        self.control_events = ControlEventRepository(session)
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        assert self._session is not None  # noqa: S101
        assert self._transaction is not None  # noqa: S101
        try:
            if exc_type is None:
                await self._transaction.commit()
            else:
                await self._transaction.rollback()
        finally:
            await self._session.close()

    async def audit(
        self, actor: str, action: str, entity: str, payload: dict[str, Any] | None = None
    ) -> None:
        """Grava no `audit_log` existente dentro da mesma transacao."""
        assert self._session is not None  # noqa: S101
        self._session.add(
            AuditLogRow(actor=actor, action=action, entity=entity, payload=to_jsonable_python(payload or {}))
        )
        await self._session.flush()

    async def record_control(
        self, run_id: str, event: ControlEvent, audit: Mapping[str, Any] | None = None
    ) -> None:
        """Evento de controle (ligar/pausar/desligar...) e sua linha de auditoria, na mesma transacao."""
        await self.control_events.append(run_id, event)
        await self.audit(
            event.operator, f"paper_{event.action.value}", run_id, {"reason": event.reason, **(audit or {})}
        )

    async def record_entry(
        self,
        run_id: str,
        *,
        order: OrderRecord,
        fill: FillRecord,
        position: ManagedPosition,
        risk_state: RiskState | None = None,
        gateway_state: Mapping[str, Any] | None = None,
    ) -> bool:
        """Ordem + fill + posicao aberta (+ estados de risco e do gateway) juntos: nunca um fill sem posicao.

        Idempotente: se o fill ja estava gravado (replay depois de queda) nada mais e tocado, para nao
        ressuscitar posicao ja encerrada nem sobrescrever estado mais novo; devolve False. True = aplicado.
        """
        await self.orders.upsert(run_id, order)
        if not await self.fills.record(run_id, fill):
            return False
        await self.positions.save_open(run_id, position)
        if risk_state is not None:
            await self.risk_states.save(run_id, risk_state, saved_at=fill.filled_at)
        if gateway_state is not None:
            await self.gateway_states.save(run_id, gateway_state, saved_at=fill.filled_at)
        return True

    async def record_exit(
        self,
        run_id: str,
        *,
        fill: FillRecord,
        trade: Trade,
        position: ManagedPosition | None,
        risk_state: RiskState,
        strategy: str | None = None,
        risk_amount: Decimal | None = None,
        order: OrderRecord | None = None,
        gateway_state: Mapping[str, Any] | None = None,
        control: ControlEvent | None = None,
    ) -> int | None:
        """Fill + posicao (atualizada ou fechada) + trade + estados de risco e do gateway, tudo ou nada.

        `control` (ex.: o HALT causado por este trade) entra na mesma transacao e so e gravado junto do trade.

        `position` None = posicao encerrada. Idempotente: se o trade ja foi gravado (replay depois de queda)
        nada e reaplicado e devolve None, mas o replay ainda confere ordem e fill contra o gravado (conteudo
        divergente levanta `ConflictError`); senao devolve a versao salva do estado de risco. Fills do OCO
        (`<entrada>:oco`) exigem a ordem registrada (`OrderRecord.oco_of`), passada em `order` ou ja gravada.
        """
        recorded = await self.trades.record(run_id, trade, strategy=strategy, risk_amount=risk_amount)
        if order is not None:
            await self.orders.upsert(run_id, order)
        elif recorded and await self.orders.get(run_id, fill.client_order_id) is None:
            raise PersistenceError(f"ordem {fill.client_order_id} do fill {fill.fill_id} nao esta registrada")
        await self.fills.record(run_id, fill)
        if not recorded:
            return None
        if position is None:
            if not await self.positions.close(run_id, trade.symbol, trade.closed_at):
                raise PersistenceError(f"{trade.symbol}: nao ha posicao aberta para encerrar")
        else:
            await self.positions.save_open(run_id, position)
        version = await self.risk_states.save(run_id, risk_state, saved_at=trade.closed_at)
        if gateway_state is not None:
            await self.gateway_states.save(run_id, gateway_state, saved_at=trade.closed_at)
        if control is not None:
            await self.record_control(run_id, control)
        return version
