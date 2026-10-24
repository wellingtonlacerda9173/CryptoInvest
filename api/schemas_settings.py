"""Contrato das configurações versionadas (pares, estratégias, risco, IA)."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from api.schemas_control import CommandOut, ReasonText

SectionName = Literal["pairs", "strategies", "risk", "ai"]
EffectName = Literal["increase", "decrease", "neutral", "unknown"]
ActivationName = Literal["immediate", "next_start", "none"]
ApplicationName = Literal["none", "pending", "applied", "partially_applied", "refused", "expired", "failed"]


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FieldSpecOut(_Out):
    path: str
    label: str
    kind: Literal["number", "integer", "boolean", "text", "list", "choice"]
    default: Any
    minimum: float | None = None
    maximum: float | None = None
    hard_limit: bool = Field(default=False, description="O servidor recusa valores acima do teto rígido.")
    unit: Literal["%", ""] = ""
    choices: list[str] | None = None


class HardLimitOut(_Out):
    path: str
    label: str
    kind: Literal["max", "min"]
    value: float
    text: str


class FieldChangeOut(_Out):
    path: str
    label: str
    before: Any
    after: Any
    before_text: str
    after_text: str
    effect: EffectName
    increases_risk: bool
    cost_text: str
    applies: Literal["now", "next_start"] = Field(description="Quando a mudança passa a valer no robô.")


class SettingsVersionOut(_Out):
    id: int
    parent_id: int | None
    created_at: datetime
    actor: str
    reason: str
    sections: list[str]
    activation: Literal["immediate", "next_start"]
    risk_increase: bool
    config_hash: str
    chain_hash: str
    changes: list[FieldChangeOut]
    application: ApplicationName = Field(
        description="Situação da redução de risco enviada ao robô: aplicada, parcial, recusada, expirada..."
    )
    application_text: str


class EffectiveConfigOut(_Out):
    config_hash: str | None
    version_id: int | None
    runner_connected: bool
    matches_latest: bool | None = Field(description="Falso: há mudanças aguardando o próximo início.")
    restart_rule: str


class SettingsOut(_Out):
    version: SettingsVersionOut | None
    document: dict[str, dict[str, Any]]
    defaults: dict[str, dict[str, Any]]
    fields: dict[str, list[FieldSpecOut]]
    hard_limits: list[HardLimitOut]
    effective: EffectiveConfigOut
    can_edit: bool
    capital_reference: float


class SettingsChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: dict[str, Any] = Field(description="Valores completos da seção (como em `GET /settings`).")


class SettingsValidateRequest(SettingsChangeRequest):
    section: SectionName


class SettingsErrorOut(_Out):
    path: str
    message: str


class SettingsValidationOut(_Out):
    valid: bool
    errors: list[SettingsErrorOut]
    changes: list[FieldChangeOut]
    increases_risk: bool
    requires_acknowledgement: bool
    activation: ActivationName
    activation_text: str
    config_hash: str | None


class SettingsUpdateRequest(SettingsChangeRequest):
    reason: ReasonText
    expected_version: int = Field(
        ge=0,
        description="OBRIGATÓRIO: versão que você estava editando (0 se ainda não há nenhuma). Evita "
        "sobrescrever mudança alheia.",
    )
    acknowledge_risk_increase: bool = Field(
        default=False, description="Segundo clique: obrigatório quando a mudança aumenta o risco."
    )


class SettingsUpdateOut(_Out):
    version: SettingsVersionOut
    activation: ActivationName
    activation_text: str
    applied_now: list[str]
    pending_restart: list[str]
    command: CommandOut | None


class SettingsHistoryPage(_Out):
    items: list[SettingsVersionOut]
    next_cursor: int | None = None
