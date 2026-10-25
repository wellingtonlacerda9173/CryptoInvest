"""Contrato do controle do robô: comandos, estado de entrega e ativação do Live (bloqueada nesta fase)."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from engine.application.live_authorization import LIVE_PHRASE

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
    """Pedido de AUTORIZACAO do Live. Todos os campos sao opcionais: o servidor lista o que faltar (409)."""

    model_config = ConfigDict(extra="ignore")

    phrase: str | None = Field(default=None, max_length=64, description=f"Frase exata: {LIVE_PHRASE}")
    confirm_real_money: bool = Field(default=False, description="Confirmacao explicita do dinheiro real")
    initial_capital_quote: Decimal | None = Field(
        default=None, description="Capital inicial reduzido do Live, na moeda de cotacao"
    )
    reason: str | None = Field(default=None, min_length=3, max_length=280, description="Motivo da ativacao")
    run_id: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    credential_id: uuid.UUID | None = Field(default=None, description="Chave; padrao: a mais recente")
    code: str | None = Field(default=None, max_length=12, description="Ignorado (legado)")


class LiveRevocationRequest(BaseModel):
    reason: ReasonText


class LiveAuthorizationOut(_Out):
    id: str
    paper_run_id: str
    actor: str
    reason: str
    initial_capital_quote: Decimal
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None
    revoked_by: str | None
    revocation_reason: str | None
    active: bool
    record_hash: str
    criteria: dict[str, Any]


class LiveAuthorizationStateOut(_Out):
    """`live_available` segue falso: autorizar nao inicia nada (a integracao com o runner e outra etapa)."""

    authorization: LiveAuthorizationOut | None
    stage: str
    days_in_live: int
    limit_increase_allowed: bool
    next_step: str
    reduced_capital_days: int
    limit_increase_days: int
    live_available: bool


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
