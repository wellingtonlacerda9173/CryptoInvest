"""Configurações versionadas. Leitura: leitor e admin. Gravação: admin com reautenticação recente."""

from typing import Annotated

from fastapi import APIRouter, Query

from api.deps import AdminDep, AuditDep, ContainerDep, FreshAdminDep, MetaDep, UserDep
from api.errors import PROBLEM_RESPONSES
from api.schemas_settings import (
    SectionName,
    SettingsHistoryPage,
    SettingsOut,
    SettingsUpdateOut,
    SettingsUpdateRequest,
    SettingsValidateRequest,
    SettingsValidationOut,
)
from api.services.settings_service import SettingsService

router = APIRouter(prefix="/settings", tags=["settings"], responses=PROBLEM_RESPONSES)


@router.get(
    "",
    operation_id="getSettings",
    response_model=SettingsOut,
    summary="Configuração vigente, padrões recomendados, tetos rígidos e o que o robô está usando",
)
async def get_settings(principal: UserDep, container: ContainerDep) -> SettingsOut:
    return await SettingsService(container).read(can_edit=principal.role == "admin")


@router.get(
    "/history",
    operation_id="listSettingsHistory",
    response_model=SettingsHistoryPage,
    summary="Versões anteriores: quem mudou, quando, por quê e o diff",
)
async def list_settings_history(
    _: UserDep,
    container: ContainerDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: Annotated[int | None, Query(ge=1)] = None,
) -> SettingsHistoryPage:
    return await SettingsService(container).history(limit=limit, cursor=cursor)


@router.post(
    "/validate",
    operation_id="validateSettings",
    response_model=SettingsValidationOut,
    summary="Valida sem salvar: erros por campo e, por campo, se a mudança aumenta o risco",
)
async def validate_settings(
    body: SettingsValidateRequest, _: AdminDep, container: ContainerDep
) -> SettingsValidationOut:
    return await SettingsService(container).validate(body.section, body.value)


@router.put(
    "/{section}",
    operation_id="updateSettings",
    response_model=SettingsUpdateOut,
    summary="Grava uma seção como nova versão (reduções valem já; aumentos, só no próximo início)",
)
async def update_settings(
    section: SectionName,
    body: SettingsUpdateRequest,
    principal: FreshAdminDep,
    container: ContainerDep,
    audit: AuditDep,
    meta: MetaDep,
) -> SettingsUpdateOut:
    return await SettingsService(container, audit).update(
        section=section,
        value=body.value,
        reason=body.reason,
        expected_version=body.expected_version,
        acknowledge=body.acknowledge_risk_increase,
        actor=principal.username,
        operator=principal.username,
        meta=meta,
    )
