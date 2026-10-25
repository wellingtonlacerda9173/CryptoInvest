"""Registro de chamadas ao analista (auditoria e contador de gasto do mes)."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal, Protocol, runtime_checkable

from pydantic import Field

from engine.application.records import Record

CallResult = Literal["ok", "invalid", "timeout", "budget", "error", "cancelled", "reserve"]
MAX_RESPONSE_CHARS = 2000


class AiCallRecord(Record):
    called_at: datetime
    model: str = Field(max_length=64)
    prompt_version: str = Field(max_length=32)
    prompt_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    response_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    response_text: str | None = Field(default=None, max_length=MAX_RESPONSE_CHARS)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cost_usd: Decimal = Field(default=Decimal(0), ge=0)
    latency_ms: int = Field(default=0, ge=0)
    result: CallResult
    decision_key: str | None = Field(default=None, max_length=64)
    reservation_id: int | None = None


class AiUsage(Record):
    month_start: datetime
    spent_usd: Decimal
    calls: int
    by_result: dict[str, int]


class AiCallStore(Protocol):
    async def record(self, call: AiCallRecord) -> None: ...

    async def usage(self, month_start: datetime) -> AiUsage: ...


@runtime_checkable
class ReservingStore(AiCallStore, Protocol):
    """Reserva atomica do custo de pior caso entre processos (robo, CLI e API de licoes). A linha 'reserve'
    conta como gasto ate existir uma linha de resultado com `reservation_id` apontando para ela."""

    async def reserve(self, call: AiCallRecord, *, budget: Decimal, month_start: datetime) -> int | None:
        """Id da reserva, ou None se `gasto do mes + call.cost_usd` passaria do teto."""
        ...

    async def settle(self, call: AiCallRecord, reservation_id: int) -> None: ...


def month_start(moment: datetime) -> datetime:
    utc = moment.astimezone(UTC)
    return utc.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
