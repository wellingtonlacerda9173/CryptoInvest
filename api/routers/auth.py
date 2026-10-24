from datetime import timedelta
from typing import Literal, cast

from fastapi import APIRouter, Response

from api.cookies import clear_session_cookies, set_session_cookies
from api.deps import AuditDep, ContainerDep, FreshDep, MetaDep, PendingDep, UserDep, expected_csrf
from api.errors import PROBLEM_RESPONSES
from api.schemas import (
    ChangePasswordRequest,
    CodeRequest,
    EnrollmentDone,
    EnrollmentStart,
    EnrollmentStartRequest,
    LoginRequest,
    LoginStep,
    MeOut,
    RecoveryCodesOut,
    Role,
    SecondFactorRequest,
    SessionStarted,
)
from api.services.auth import AuthService
from api.services.mapping import user_out

router = APIRouter(prefix="/auth", tags=["auth"], responses=PROBLEM_RESPONSES)


@router.post(
    "/login",
    operation_id="login",
    response_model=LoginStep,
    summary="Passo 1: usuario e senha",
    description="Confere a senha e abre um login pendente (cookie de sessao curto). O acesso so e liberado "
    "depois do segundo passo (`/auth/2fa/verify` ou a configuracao do 2FA).",
)
async def login(
    body: LoginRequest, response: Response, container: ContainerDep, audit: AuditDep, meta: MetaDep
) -> LoginStep:
    outcome = await AuthService(container, audit).login(
        username=body.username, password=body.password.get_secret_value(), meta=meta
    )
    csrf = set_session_cookies(response, container.settings, outcome.started, container.session_secret)
    return LoginStep(
        stage=cast("Literal['totp', 'enrollment']", outcome.stage),
        csrf_token=csrf,
        expires_in_seconds=container.settings.pending_login_minutes * 60,
    )


@router.post(
    "/2fa/verify",
    operation_id="verifySecondFactor",
    response_model=SessionStarted,
    summary="Passo 2: codigo do app autenticador ou codigo de recuperacao",
)
async def verify_second_factor(
    body: SecondFactorRequest,
    response: Response,
    principal: PendingDep,
    container: ContainerDep,
    audit: AuditDep,
    meta: MetaDep,
) -> SessionStarted:
    activated = await AuthService(container, audit).verify_second_factor(
        principal, code=body.code, recovery_code=body.recovery_code, meta=meta
    )
    csrf = set_session_cookies(response, container.settings, activated.started, container.session_secret)
    return SessionStarted(
        user=user_out(activated.user),
        csrf_token=csrf,
        expires_at=activated.started.expires_at,
        recovery_code_used=activated.recovery_code_used,
    )


@router.post(
    "/2fa/enroll",
    operation_id="beginTwoFactorEnrollment",
    response_model=EnrollmentStart,
    summary="Gera o segredo TOTP (otpauth + QR); exige o token de ativacao entregue pelo administrador",
)
async def begin_enrollment(
    body: EnrollmentStartRequest,
    principal: PendingDep,
    container: ContainerDep,
    audit: AuditDep,
    meta: MetaDep,
) -> EnrollmentStart:
    info = await AuthService(container, audit).begin_enrollment(
        principal, activation_token=body.activation_token.get_secret_value(), meta=meta
    )
    return EnrollmentStart(otpauth_uri=info.uri, secret=info.secret, qr_svg_data_uri=info.qr_data_uri)


@router.post(
    "/2fa/enroll/confirm",
    operation_id="confirmTwoFactorEnrollment",
    response_model=EnrollmentDone,
    summary="Confirma o primeiro codigo, ativa o 2FA e devolve os codigos de recuperacao (uma unica vez)",
)
async def confirm_enrollment(
    body: CodeRequest,
    response: Response,
    principal: PendingDep,
    container: ContainerDep,
    audit: AuditDep,
    meta: MetaDep,
) -> EnrollmentDone:
    activated = await AuthService(container, audit).confirm_enrollment(principal, code=body.code, meta=meta)
    csrf = set_session_cookies(response, container.settings, activated.started, container.session_secret)
    return EnrollmentDone(
        user=user_out(activated.user),
        csrf_token=csrf,
        expires_at=activated.started.expires_at,
        recovery_codes=activated.recovery_codes,
    )


@router.post("/logout", operation_id="logout", status_code=204, summary="Encerra e revoga a sessao atual")
async def logout(
    response: Response, principal: UserDep, container: ContainerDep, audit: AuditDep, meta: MetaDep
) -> None:
    await AuthService(container, audit).logout(principal, meta)
    clear_session_cookies(response, container.settings)


@router.get("/me", operation_id="getCurrentUser", response_model=MeOut, summary="Usuario da sessao atual")
async def me(principal: UserDep, container: ContainerDep) -> MeOut:
    fresh = principal.fresh_auth_at
    window = timedelta(minutes=container.settings.fresh_auth_minutes)
    return MeOut(
        id=principal.user_id,
        username=principal.username,
        role=cast("Role", principal.role),
        totp_enabled=principal.totp_enabled,
        fresh_auth_until=(fresh + window) if fresh and container.clock() - fresh <= window else None,
        csrf_token=expected_csrf(container, principal),
    )


@router.post(
    "/reauth",
    operation_id="reauthenticate",
    response_model=MeOut,
    summary="Reautentica com codigo TOTP para liberar acoes sensiveis por alguns minutos",
)
async def reauthenticate(
    body: CodeRequest, principal: UserDep, container: ContainerDep, audit: AuditDep, meta: MetaDep
) -> MeOut:
    now = await AuthService(container, audit).reauthenticate(principal, code=body.code, meta=meta)
    return MeOut(
        id=principal.user_id,
        username=principal.username,
        role=cast("Role", principal.role),
        totp_enabled=principal.totp_enabled,
        fresh_auth_until=now + timedelta(minutes=container.settings.fresh_auth_minutes),
        csrf_token=expected_csrf(container, principal),
    )


@router.post(
    "/password",
    operation_id="changePassword",
    status_code=204,
    summary="Troca a senha (exige a senha atual) e revoga as outras sessoes",
)
async def change_password(
    body: ChangePasswordRequest, principal: UserDep, container: ContainerDep, audit: AuditDep, meta: MetaDep
) -> None:
    await AuthService(container, audit).change_password(
        principal,
        current=body.current_password.get_secret_value(),
        new=body.new_password.get_secret_value(),
        meta=meta,
    )


@router.post(
    "/2fa/recovery-codes",
    operation_id="regenerateRecoveryCodes",
    response_model=RecoveryCodesOut,
    summary="Gera novos codigos de recuperacao (invalida os anteriores); exige reautenticacao recente",
)
async def regenerate_recovery_codes(
    principal: FreshDep, container: ContainerDep, audit: AuditDep, meta: MetaDep
) -> RecoveryCodesOut:
    return RecoveryCodesOut(
        recovery_codes=await AuthService(container, audit).regenerate_recovery_codes(principal, meta)
    )
