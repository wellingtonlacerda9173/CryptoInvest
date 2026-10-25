from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from engine.domain.order import EntryOrder
from engine.domain.types import Symbol

RecoveryOutcome = Literal[
    "working", "protected", "reprotected", "flat", "exposed", "not_placed", "already_known"
]


@dataclass(frozen=True)
class PersistedEntry:
    """Unico estado que o chamador persiste por entrada. Todos os ids de ordem sao deterministicos
    (`derived_id`), entao o resto e reconstruido na corretora.

    `generation`: ultima geracao de protecao conhecida; pode estar ATRASADA (o gateway sonda algumas
    geracoes alem dela). `exit_client_ids`: ids das saidas (`ExitOrder`) ja enviadas para esta entrada: sao
    escolhidos pelo chamador, logo nao sao derivaveis; saida omitida nao e abatida do ledger.
    """

    order: EntryOrder
    expires_at: datetime
    generation: int = 0
    exit_client_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class RecoveredEntry:
    entry_id: str
    outcome: RecoveryOutcome
    detail: str


@dataclass(frozen=True)
class RecoveryResult:
    entries: tuple[RecoveredEntry, ...]

    @property
    def ok(self) -> bool:
        return all(e.outcome != "exposed" for e in self.entries)


@dataclass(frozen=True)
class OperatorConfirmation:
    """Confirmacao explicita do operador para `close_orphan`: quem, qual quantidade e por que."""

    operator: str
    confirmed_quantity: Decimal
    reason: str


@dataclass(frozen=True)
class OrphanCloseResult:
    symbol: Symbol
    requested: Decimal
    applied: Decimal
    executed: Decimal
    order_ids: tuple[str, ...]
