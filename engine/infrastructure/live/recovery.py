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
    geracoes alem dela e sonda para cima enquanto achar ordens). `stop_loss`/`target_dropped`: ultimo nivel
    anunciado por `ProtectionIntent` (opcional). `exit_client_ids`: ids das saidas (`ExitOrder`) ja enviadas
    para esta entrada: sao escolhidos pelo chamador, logo nao sao derivaveis;
    saida omitida nao e abatida do ledger. `acknowledged_trade_ids`: ids dos trades (`/myTrades`) de venda
    sem ordem conhecida que o OPERADOR confirmou serem alheios a esta posicao (nao abatem o `owed`); a
    confirmacao vale SOMENTE para esses ids: qualquer outro trade novo continua recusado (pausa + alerta).
    `acknowledged_by`: quem confirmou (vai para o log de auditoria).
    """

    order: EntryOrder
    expires_at: datetime
    generation: int = 0
    exit_client_ids: tuple[str, ...] = ()
    stop_loss: Decimal | None = None
    target_dropped: bool = False
    acknowledged_trade_ids: tuple[int, ...] = ()
    acknowledged_by: str = ""


@dataclass(frozen=True)
class ProtectionIntent:
    """Nivel de protecao que o gateway esta prestes a (re)colocar, entregue ANTES de cancelar a protecao
    vigente (write-ahead). O chamador persiste e devolve em `PersistedEntry` (`generation`, `stop_loss`,
    `target_dropped`): sem isso, uma queda entre o DELETE e o POST do `raise_stop` so permite reproteger no
    ultimo stop que existiu na corretora (mais baixo, mas nunca sem protecao)."""

    entry_id: str
    generation: int
    stop_loss: Decimal
    target: Decimal | None

    def persisted(self, base: PersistedEntry) -> PersistedEntry:
        return PersistedEntry(
            order=base.order,
            expires_at=base.expires_at,
            generation=self.generation,
            exit_client_ids=base.exit_client_ids,
            stop_loss=self.stop_loss,
            target_dropped=self.target is None,
            acknowledged_trade_ids=base.acknowledged_trade_ids,
            acknowledged_by=base.acknowledged_by,
        )


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
