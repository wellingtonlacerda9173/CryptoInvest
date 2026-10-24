import uuid

from fastapi import APIRouter

from api.deps import AdminDep, AuditDep, ContainerDep, FreshAdminDep, MetaDep
from api.errors import PROBLEM_RESPONSES
from api.schemas import CredentialCreate, CredentialOut, CredentialRotate
from api.services.credentials import CredentialService, to_out
from api.services.guards import ensure_robot_stopped

router = APIRouter(prefix="/credentials", tags=["credentials"], responses=PROBLEM_RESPONSES)


@router.get(
    "",
    operation_id="listCredentials",
    response_model=list[CredentialOut],
    summary="Chaves da corretora: so metadados, nunca o segredo (admin)",
)
async def list_credentials(_: AdminDep, container: ContainerDep, audit: AuditDep) -> list[CredentialOut]:
    return [to_out(row) for row in await CredentialService(container, audit).list()]


@router.post(
    "",
    operation_id="createCredential",
    response_model=CredentialOut,
    status_code=201,
    summary="Grava uma chave (write-only). Recusa chaves com permissao de saque",
)
async def create_credential(
    body: CredentialCreate, principal: FreshAdminDep, container: ContainerDep, audit: AuditDep, meta: MetaDep
) -> CredentialOut:
    row = await CredentialService(container, audit).create(
        label=body.label,
        api_key=body.api_key,
        api_secret=body.api_secret,
        actor=principal.username,
        actor_id=principal.user_id,
        meta=meta,
    )
    return to_out(row)


@router.put(
    "/{credential_id}",
    operation_id="rotateCredential",
    response_model=CredentialOut,
    summary="Troca a chave e o segredo (rotacao); revalida as permissoes",
)
async def rotate_credential(
    credential_id: uuid.UUID,
    body: CredentialRotate,
    principal: FreshAdminDep,
    container: ContainerDep,
    audit: AuditDep,
    meta: MetaDep,
) -> CredentialOut:
    row = await CredentialService(container, audit).rotate(
        credential_id=credential_id,
        api_key=body.api_key,
        api_secret=body.api_secret,
        actor=principal.username,
        meta=meta,
    )
    return to_out(row)


@router.delete("/{credential_id}", operation_id="deleteCredential", status_code=204, summary="Remove a chave")
async def delete_credential(
    credential_id: uuid.UUID,
    principal: FreshAdminDep,
    container: ContainerDep,
    audit: AuditDep,
    meta: MetaDep,
) -> None:
    await ensure_robot_stopped(
        container, audit, actor=principal.username, target=f"credential:{credential_id}", meta=meta
    )
    await CredentialService(container, audit).delete(
        credential_id=credential_id, actor=principal.username, meta=meta
    )


@router.post(
    "/{credential_id}/verify",
    operation_id="verifyCredential",
    response_model=CredentialOut,
    summary="Testa a chave guardada e atualiza as permissoes detectadas",
)
async def verify_credential(
    credential_id: uuid.UUID, principal: AdminDep, container: ContainerDep, audit: AuditDep, meta: MetaDep
) -> CredentialOut:
    row = await CredentialService(container, audit).verify_stored(
        credential_id=credential_id, actor=principal.username, meta=meta
    )
    return to_out(row)
