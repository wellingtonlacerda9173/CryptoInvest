"""Contrato do controle do robô: comandos, estado de entrega e ativação do Live (bloqueada nesta fase)."""

from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

CommandState = Literal[
    "pending_delivery",
    "delivered",
    "applied",
    "partially_applied",
    "refused",
    "discarded",
    "expired",
    "failed",
]
CommandKind = Literal["pause", "resume", "stop", "emergency-stop", "reactivate", "apply-config"]
OperatorName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=60)]
ReasonText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=280)]

LIVE_PHRASE = "OPERAR COM DINHEIRO REAL"
LIVE_UNAVAILABLE = "Live ainda não disponível"


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CommandOut(_Out):
    id: str
    name: CommandKind
    operator: str
    reason: str
    liquidate: bool
    state: CommandState
    state_text: str = Field(examples=["Pendente de entrega: o robô está sem conexão com o painel."])
    channel: Literal["redis", "file"] | None
    created_at: datetime
    expires_at: datetime | None = Field(description="Nulo: o comando nunca expira (parada de emergência).")
    delivered_at: datetime | None
    resolved_at: datetime | None
    detail: str | None = None


class CommandAccepted(CommandOut):
    message: str


class CommandPage(_Out):
    items: list[CommandOut]
    next_cursor: str | None = None


class _In(BaseModel):
    """Corpo de controle: campo desconhecido é erro (um `liquidat: true` com typo nunca passa em branco)."""

    model_config = ConfigDict(extra="forbid")


class PauseRequest(_In):
    reason: ReasonText = Field(examples=["Vou mexer na corretora"])
    operator_name: OperatorName | None = Field(default=None, description="Nome de quem pede (opcional).")


class ResumeRequest(_In):
    reason: ReasonText
    operator_name: OperatorName


class StopRequest(_In):
    reason: ReasonText = Field(default="Parada ordenada pelo painel")
    operator_name: OperatorName | None = None


class EmergencyStopRequest(_In):
    liquidate: bool = Field(default=False, description="Falso: só bloqueia novas operações (recomendado).")
    reason: ReasonText | None = None
    operator_name: OperatorName | None = None

    @model_validator(mode="after")
    def _identify_when_selling(self) -> Self:
        if self.liquidate and not (self.reason and self.operator_name):
            raise ValueError("para vender tudo informe `operator_name` e `reason`")
        return self


class ReactivateRequest(_In):
    reason: ReasonText
    operator_name: OperatorName


class PowerRequest(_In):
    on: bool
    reason: ReasonText = Field(default="Ligar/desligar pelo painel")
    operator_name: OperatorName | None = None


class PowerOut(_Out):
    state: Literal["commanded", "launched", "waiting_runner"]
    message: str
    command: CommandAccepted | None = None


class LiveActivationRequest(BaseModel):
    """Aceita qualquer corpo: nesta fase a resposta é sempre 409 e o modo nunca muda."""

    phrase: str | None = Field(default=None, max_length=64, description=f"Frase fixa: {LIVE_PHRASE}")
    code: str | None = Field(default=None, max_length=12)


class OnboardingStepOut(_Out):
    key: Literal["admin", "two_factor", "binance_key", "risk", "paper"]
    title: str
    done: bool
    required: bool
    detail: str


class OnboardingOut(_Out):
    steps: list[OnboardingStepOut]
    completed: bool
    next_step: str | None
    can_start_paper: bool
    paper_running: bool
    message: str


class StartPaperOut(_Out):
    state: Literal["started", "already_running", "resumed", "waiting_runner", "launched"]
    message: str
    config_version: int | None
    command_id: str | None = None
