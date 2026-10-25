from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


CalendarState = Literal["ok", "expiring", "expired"]
UpdateResult = Literal["updated", "unchanged", "rejected", "failed", "never"]
EventKind = Literal["fomc", "cpi", "payroll", "deribit", "other"]


class CalendarEventOut(Out):
    start: datetime
    end: datetime
    label: str
    kind: EventKind = "other"
    source: str
    block_start: datetime = Field(
        description="Inicio EFETIVO da pausa do robo: inicio da janela menos a margem."
    )
    block_end: datetime = Field(description="Fim EFETIVO da pausa (inclusivo): fim da janela mais a margem.")


class CalendarUpdateOut(Out):
    attempted_at: datetime | None = None
    result: UpdateResult = "never"


class CalendarOut(Out):
    state: CalendarState = Field(
        description="ok; expiring (menos de 14 dias de cobertura); expired (sem cobertura)."
    )
    now: datetime = Field(description="Relogio do servidor: base para dias restantes e janela ativa.")
    loaded_at: datetime | None = None
    last_success_at: datetime | None = Field(
        default=None, description="Ultima execucao do atualizador que terminou bem (atualizou ou confirmou)."
    )
    valid_until: datetime | None = None
    days_remaining: int | None = Field(
        default=None, description="Dias inteiros de cobertura restantes (0 se vencido)."
    )
    margin_before_minutes: int = Field(description="Margem de pausa antes de cada evento, em uso pelo robo.")
    margin_after_minutes: int = Field(description="Margem de pausa depois de cada evento, em uso pelo robo.")
    upcoming: list[CalendarEventOut] = Field(
        description="Proximas janelas de eventos (ate 10), ordenadas por inicio; inclui as ainda em pausa."
    )
    last_update: CalendarUpdateOut
