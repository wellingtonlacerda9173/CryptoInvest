from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from engine.application.risk import ManagedPosition, RiskState
from engine.domain.journal import OrderRecord, RunRecord
from engine.infrastructure.persistence.order_repository import OrderRepository
from engine.infrastructure.persistence.position_repository import PositionRepository
from engine.infrastructure.persistence.run_repository import RunRepository
from engine.infrastructure.persistence.state_repository import (
    GatewaySnapshot,
    GatewayStateRepository,
    RiskStateRepository,
)


@dataclass(frozen=True, slots=True)
class RecoveryState:
    """Tudo que o robo precisa para retomar um run apos queda; os campos ausentes indicam run novo."""

    run: RunRecord | None
    risk_state: RiskState | None
    gateway_state: GatewaySnapshot | None
    open_positions: tuple[ManagedPosition, ...]
    open_orders: tuple[OrderRecord, ...]


async def load_recovery_state(sessions: async_sessionmaker[AsyncSession], run_id: str) -> RecoveryState:
    """Leitura consistente (uma unica foto REPEATABLE READ): estado, posicoes e ordens da mesma instancia."""
    async with sessions() as session:
        await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        return RecoveryState(
            run=await RunRepository(session).get(run_id),
            risk_state=await RiskStateRepository(session).load_latest(run_id),
            gateway_state=await GatewayStateRepository(session).load_latest(run_id),
            open_positions=tuple(await PositionRepository(session).list_open(run_id)),
            open_orders=tuple(await OrderRepository(session).list_open(run_id)),
        )
