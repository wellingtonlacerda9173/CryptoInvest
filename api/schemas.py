import unicodedata
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    model_validator,
)

from api.binance_keys import KeyPermissions

Role = Literal["admin", "reader"]
Username = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=64)]
# Preenchedores que renderizam como vazio mas nao sao "format"/"control" para o Unicode
_INVISIBLE = frozenset("\u115f\u1160\u2800\u3164\uffa0\u180e\u034f\u061c")


def _plain_label(value: str) -> str:
    """Rotulo do painel/auditoria: sem controle, bidi/RTL, invisiveis nem separadores exoticos."""
    for char in value:
        category = unicodedata.category(char)
        if (
            category in {"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"}
            or (category == "Zs" and char != " ")
            or char in _INVISIBLE
        ):
            raise ValueError("o rotulo contem caracteres de controle, de direcao ou invisiveis")
    return value


Label = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64), AfterValidator(_plain_label)
]
Password = Annotated[SecretStr, Field(min_length=1, max_length=256, examples=["frase longa e unica 2026!"])]
OneTimeCode = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=6, max_length=9, pattern=r"^[0-9 ]+$")
]
ApiKeyText = Annotated[SecretStr, Field(min_length=16, max_length=128, examples=["****************"])]


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UserOut(_Out):
    id: uuid.UUID
    username: str = Field(examples=["ana.silva"])
    role: Role
    totp_enabled: bool
    disabled: bool
    created_at: datetime
    locked_until: datetime | None = None


class MeOut(_Out):
    id: uuid.UUID
    username: str = Field(examples=["ana.silva"])
    role: Role
    totp_enabled: bool
    fresh_auth_until: datetime | None = Field(
        default=None, description="Ate quando acoes sensiveis dispensam novo codigo 2FA."
    )
    csrf_token: str = Field(description="Token a enviar no header X-CSRF-Token em metodos mutaveis.")


class LoginRequest(BaseModel):
    username: Username = Field(examples=["ana.silva"])
    password: Password


class LoginStep(_Out):
    stage: Literal["totp", "enrollment"] = Field(
        description="`totp`: informar o codigo do app. `enrollment`: configurar o 2FA antes de entrar."
    )
    csrf_token: str
    expires_in_seconds: int = Field(examples=[300])


class SecondFactorRequest(BaseModel):
    code: OneTimeCode | None = Field(default=None, examples=["123456"])
    recovery_code: str | None = Field(default=None, max_length=32, examples=["ABCD-EFGH-JKLM-NPQR"])

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        if (self.code is None) == (self.recovery_code is None):
            raise ValueError("informe `code` ou `recovery_code`")
        return self


class CodeRequest(BaseModel):
    code: OneTimeCode = Field(examples=["123456"])


class SessionStarted(_Out):
    user: UserOut
    csrf_token: str
    expires_at: datetime
    recovery_code_used: bool = False


class EnrollmentStartRequest(BaseModel):
    activation_token: Annotated[SecretStr, Field(min_length=16, max_length=64)] = Field(
        description="Token de ativacao entregue pelo administrador (uso unico, validade limitada)."
    )


class ActivationOut(_Out):
    activation_token: str = Field(
        description="Mostrado UMA unica vez; repasse ao usuario por canal separado da senha."
    )
    activation_expires_at: datetime


class UserCreated(UserOut):
    activation_token: str = Field(
        description="Mostrado UMA unica vez; exigido no primeiro enrollment do 2FA do usuario."
    )
    activation_expires_at: datetime


class EnrollmentStart(_Out):
    otpauth_uri: str = Field(
        examples=["otpauth://totp/CryptoInvest:ana.silva?secret=JBSWY3DPEHPK3PXP&issuer=CryptoInvest"]
    )
    secret: str = Field(
        description="Segredo para digitacao manual; so aparece neste passo.", examples=["JBSWY3DPEHPK3PXP"]
    )
    qr_svg_data_uri: str = Field(description="QR code em `data:image/svg+xml;base64,...` para uso em <img>.")


class EnrollmentDone(_Out):
    user: UserOut
    csrf_token: str
    expires_at: datetime
    recovery_codes: list[str] = Field(
        description="Mostrados uma unica vez.", examples=[["ABCD-EFGH-JKLM-NPQR"]]
    )


class RecoveryCodesOut(_Out):
    recovery_codes: list[str] = Field(description="Mostrados uma unica vez; substituem os anteriores.")


class ChangePasswordRequest(BaseModel):
    current_password: Password
    new_password: Password


class SessionOut(_Out):
    id: uuid.UUID
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    ip: str | None
    user_agent: str | None
    current: bool


class CreateUserRequest(BaseModel):
    username: Username = Field(examples=["joao.leitor"])
    password: Password
    role: Role = "reader"


class UpdateUserRequest(BaseModel):
    role: Role | None = None
    disabled: bool | None = None

    @model_validator(mode="after")
    def _something(self) -> Self:
        if self.role is None and self.disabled is None:
            raise ValueError("informe `role` e/ou `disabled`")
        return self


class AuditEntryOut(_Out):
    id: int
    occurred_at: datetime
    actor: str = Field(examples=["ana.silva"])
    action: str = Field(examples=["auth.login.succeeded"])
    target: str = Field(examples=["user:3f2b9c1d"])
    result: Literal["success", "failure", "denied"] | None
    ip: str | None
    user_agent: str | None
    request_id: str | None
    details: dict[str, Any]


class AuditPage(_Out):
    items: list[AuditEntryOut]
    next_cursor: int | None = Field(default=None, description="Passe em `cursor` para a pagina seguinte.")


class CredentialCreate(BaseModel):
    exchange: Literal["binance"] = "binance"
    label: Label = Field(examples=["Conta principal"])
    api_key: ApiKeyText
    api_secret: ApiKeyText


class CredentialRotate(BaseModel):
    api_key: ApiKeyText
    api_secret: ApiKeyText


class CredentialOut(_Out):
    id: uuid.UUID
    exchange: Literal["binance"]
    label: str
    api_key_last4: str = Field(examples=["a91f"])
    permissions: KeyPermissions
    usable: bool = Field(description="Leitura habilitada e sem permissao de saque.")
    warnings: list[str] = []
    created_at: datetime
    updated_at: datetime
    last_verified_at: datetime | None
    last_used_at: datetime | None


class HealthOut(_Out):
    status: Literal["ok"] = "ok"


class ReadyOut(_Out):
    status: Literal["ready", "unavailable"]
    database: bool
