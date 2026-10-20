"""Estado proprio do runner, gravado junto do estado do gateway na mesma transacao."""

from collections.abc import Mapping
from typing import Any

from pydantic import Field

from engine.application.risk import EmergencyExit, ExitFill
from engine.domain.base import DomainModel
from engine.domain.journal import OrderRecord
from engine.domain.types import Price, Symbol, UtcDatetime, Volume
from engine.infrastructure.backtest import Fill
from engine.infrastructure.paper import PaperState

BLOB_VERSION = 1


class LiveSnapshot(DomainModel):
    """Posicao (ou venda de emergencia) em andamento: o que nao cabe em `ManagedPosition`."""

    entry_id: str
    symbol: Symbol
    strategy: str
    risk_amount: Volume
    gateway_stop: Price
    entry_order: OrderRecord
    partial_order: OrderRecord | None = None
    partial_fill: Fill | None = None
    closing: ExitFill | None = None
    exit_order: OrderRecord | None = None
    exit_seq: int = Field(default=0, ge=0)
    emergency: EmergencyExit | None = None


class PauseEntry(DomainModel):
    """Um motivo de pausa ativo; o robo so volta a operar quando TODOS forem resolvidos."""

    reason: str
    operator: str
    at: UtcDatetime


class RunnerSnapshot(DomainModel):
    pauses: tuple[PauseEntry, ...] = ()
    lives: tuple[LiveSnapshot, ...] = ()
    handled: dict[str, UtcDatetime] = Field(default_factory=dict)
    seen_seq: int | None = Field(default=None, ge=0)


def encode_blob(gateway: PaperState, runner: RunnerSnapshot) -> dict[str, Any]:
    return {
        "version": BLOB_VERSION,
        "gateway": gateway.model_dump(mode="json"),
        "runner": runner.model_dump(mode="json"),
    }


def decode_blob(blob: Mapping[str, Any]) -> tuple[PaperState, RunnerSnapshot]:
    if blob.get("version") != BLOB_VERSION:
        raise ValueError(f"versao de estado desconhecida: {blob.get('version')!r}")
    return PaperState.model_validate(blob["gateway"]), RunnerSnapshot.model_validate(blob.get("runner") or {})
