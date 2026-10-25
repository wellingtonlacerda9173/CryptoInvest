from typing import TYPE_CHECKING

from engine.infrastructure.live.client import BinanceRestClient
from engine.infrastructure.live.config import LiveConfig
from engine.infrastructure.live.credentials import ApiCredentials
from engine.infrastructure.live.gateway import LiveGateway, ReconciliationResult
from engine.infrastructure.live.recovery import (
    OperatorConfirmation,
    OrphanCloseResult,
    PersistedEntry,
    RecoveredEntry,
    RecoveryResult,
)

if TYPE_CHECKING:
    from engine.domain.gateway import AsyncProtectedOrderGateway

    def _conforms(gateway: LiveGateway) -> AsyncProtectedOrderGateway:
        """Verificado pelo mypy: o Live cumpre ExchangeGateway + protecao assincrona."""
        return gateway


__all__ = [
    "ApiCredentials",
    "BinanceRestClient",
    "LiveConfig",
    "LiveGateway",
    "OperatorConfirmation",
    "OrphanCloseResult",
    "PersistedEntry",
    "ReconciliationResult",
    "RecoveredEntry",
    "RecoveryResult",
]
