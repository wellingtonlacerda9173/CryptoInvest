import uuid

from fastapi import APIRouter

from api.deps import AdminDep, AuditDep, ContainerDep, FreshAdminDep, MetaDep
from api.errors import PROBLEM_RESPONSES
from api.schemas import ActivationOut, CreateUserRequest, UpdateUserRequest, UserCreated, UserOut
from api.services.mapping import user_out
from api.services.users import UserService

router = APIRouter(prefix="/users", tags=["users"], responses=PROBLEM_RESPONSES)


@router.get("", operation_id="listUsers", response_model=list[UserOut], summary="Lista usuarios (admin)")
async def list_users(_: AdminDep, container: ContainerDep, audit: AuditDep) -> list[UserOut]:
    return [user_out(user) for user in await UserService(container, audit).list()]


@router.post(
    "",
    operation_id="createUser",
    response_model=UserCreated,
    status_code=201,
    summary="Cria usuario (admin, reautenticacao recente); devolve o token de ativacao do 2FA UMA vez",
)
async def create_user(
    body: CreateUserRequest, principal: FreshAdminDep, container: ContainerDep, audit: AuditDep, meta: MetaDep
) -> UserCreated:
    created = await UserService(container, audit).create(
        username=body.username,
        password=body.password.get_secret_value(),
        role=body.role,
        actor=principal.username,
        meta=meta,
    )
    return UserCreated(
        **user_out(created.user).model_dump(),
        activation_token=created.activation.token,
        activation_expires_at=created.activation.expires_at,
    )


@router.patch(
    "/{user_id}",
    operation_id="updateUser",
    response_model=UserOut,
    summary="Altera papel e/ou desabilita usuario (admin, reauth recente); desabilitar revoga as sessoes",
)
async def update_user(
    user_id: uuid.UUID,
    body: UpdateUserRequest,
    principal: FreshAdminDep,
    container: ContainerDep,
    audit: AuditDep,
    meta: MetaDep,
) -> UserOut:
    user = await UserService(container, audit).update(
        user_id=user_id,
        role=body.role,
        disabled=body.disabled,
        actor_id=principal.user_id,
        actor=principal.username,
        meta=meta,
    )
    return user_out(user)


@router.post(
    "/{user_id}/reset-two-factor",
    operation_id="resetUserTwoFactor",
    response_model=ActivationOut,
    summary="Remove o 2FA do usuario (perda do aparelho); devolve novo token de ativacao UMA vez",
)
async def reset_two_factor(
    user_id: uuid.UUID, principal: FreshAdminDep, container: ContainerDep, audit: AuditDep, meta: MetaDep
) -> ActivationOut:
    activation = await UserService(container, audit).reset_two_factor(
        user_id=user_id, actor_id=principal.user_id, actor=principal.username, meta=meta
    )
    return ActivationOut(activation_token=activation.token, activation_expires_at=activation.expires_at)


@router.post(
    "/{user_id}/unlock",
    operation_id="unlockUser",
    response_model=UserOut,
    summary="Zera o bloqueio por tentativas do usuario (admin, reautenticacao recente); auditado",
)
async def unlock_user(
    user_id: uuid.UUID, principal: FreshAdminDep, container: ContainerDep, audit: AuditDep, meta: MetaDep
) -> UserOut:
    user = await UserService(container, audit).unlock(user_id=user_id, actor=principal.username, meta=meta)
    return user_out(user)
