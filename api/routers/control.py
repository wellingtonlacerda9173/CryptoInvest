"""Controle do robô (admin). A API NÃO decide nada de trading: só enfileira comandos assinados ao runner.

Pausar, retomar, parar e a parada de emergência não pedem 2FA (reduzem risco e nunca podem atrasar);
reativar após HALTED e ativar o Live pedem reautenticação recente.
"""

from typing import Annotated

from fastapi import APIRouter, Query

from api.deps import AdminDep, AuditDep, ContainerDep, FreshAdminDep, MetaDep, UserDep
from api.errors import PROBLEM_RESPONSES, Conflict
from api.schemas_control import (
    CommandAccepted,
    CommandOut,
    CommandPage,
    EmergencyStopRequest,
    LiveActivationRequest,
    LiveAuthorizationOut,
    LiveAuthorizationStateOut,
    LiveRevocationRequest,
    PauseRequest,
    PowerOut,
    PowerRequest,
    ReactivateRequest,
    ResumeRequest,
    StopRequest,
)
from api.services.commands import CommandService, operator_label
from api.services.launcher import launch_runner
from api.services.live_activation import LiveActivationService
from api.services.runner_status import RunnerSnapshot, RunnerStatusService
from engine.runner.paper_control import CommandName

router = APIRouter(tags=["control"], responses=PROBLEM_RESPONSES)


async def _send(
    name: CommandName,
    *,
    principal_name: str,
    display: str | None,
    reason: str,
    container: ContainerDep,
    audit: AuditDep,
    meta: MetaDep,
    liquidate: bool = False,
) -> CommandAccepted:
    return await CommandService(container, audit).submit(
        name=name,
        operator=operator_label(principal_name, display),
        reason=reason,
        actor=principal_name,
        meta=meta,
        liquidate=liquidate,
    )


async def _alive(container: ContainerDep) -> RunnerSnapshot:
    return await RunnerStatusService(container).snapshot()


@router.post(
    "/control/pause",
    operation_id="pauseBot",
    status_code=202,
    response_model=CommandAccepted,
    summary="Pausa novas entradas (posições abertas seguem protegidas)",
)
async def pause_bot(
    body: PauseRequest, principal: AdminDep, container: ContainerDep, audit: AuditDep, meta: MetaDep
) -> CommandAccepted:
    return await _send(
        CommandName.PAUSE,
        principal_name=principal.username,
        display=body.operator_name,
        reason=body.reason,
        container=container,
        audit=audit,
        meta=meta,
    )


@router.post(
    "/control/resume",
    operation_id="resumeBot",
    status_code=202,
    response_model=CommandAccepted,
    summary="Retoma de uma pausa (nunca reativa o robô desligado por drawdown)",
)
async def resume_bot(
    body: ResumeRequest, principal: AdminDep, container: ContainerDep, audit: AuditDep, meta: MetaDep
) -> CommandAccepted:
    snapshot = await _alive(container)
    if snapshot.alive and snapshot.data and snapshot.data.get("halted"):
        raise Conflict(
            "O robô está desligado por drawdown. Use Reativar, que exige o código do app autenticador."
        )
    return await _send(
        CommandName.RESUME,
        principal_name=principal.username,
        display=body.operator_name,
        reason=body.reason,
        container=container,
        audit=audit,
        meta=meta,
    )


@router.post(
    "/control/stop",
    operation_id="stopBot",
    status_code=202,
    response_model=CommandAccepted,
    summary="Parada ordenada: sem novas entradas, estado salvo, stops mantidos",
)
async def stop_bot(
    body: StopRequest, principal: AdminDep, container: ContainerDep, audit: AuditDep, meta: MetaDep
) -> CommandAccepted:
    return await _send(
        CommandName.STOP,
        principal_name=principal.username,
        display=body.operator_name,
        reason=body.reason,
        container=container,
        audit=audit,
        meta=meta,
    )


@router.post(
    "/control/emergency-stop",
    operation_id="emergencyStop",
    status_code=202,
    response_model=CommandAccepted,
    summary="Parar tudo: bloqueia novas operações; `liquidate` vende a mercado (exige nome e motivo)",
)
async def emergency_stop(
    body: EmergencyStopRequest, principal: AdminDep, container: ContainerDep, audit: AuditDep, meta: MetaDep
) -> CommandAccepted:
    reason = body.reason or (
        "Parar tudo: bloquear e vender" if body.liquidate else "Parar tudo: só bloquear novas operações"
    )
    return await _send(
        CommandName.EMERGENCY_STOP,
        principal_name=principal.username,
        display=body.operator_name,
        reason=reason,
        container=container,
        audit=audit,
        meta=meta,
        liquidate=body.liquidate,
    )


@router.post(
    "/control/reactivate",
    operation_id="reactivateBot",
    status_code=202,
    response_model=CommandAccepted,
    summary="Reativa o robô desligado por drawdown (nome, motivo e 2FA recente)",
)
async def reactivate_bot(
    body: ReactivateRequest, principal: FreshAdminDep, container: ContainerDep, audit: AuditDep, meta: MetaDep
) -> CommandAccepted:
    snapshot = await _alive(container)
    if snapshot.alive and snapshot.data and not snapshot.data.get("halted"):
        raise Conflict("O robô não está desligado por drawdown: não há o que reativar.")
    return await _send(
        CommandName.REACTIVATE,
        principal_name=principal.username,
        display=body.operator_name,
        reason=body.reason,
        container=container,
        audit=audit,
        meta=meta,
    )


@router.post(
    "/control/power",
    operation_id="setBotPower",
    status_code=202,
    response_model=PowerOut,
    summary="Liga ou desliga o robô (desligar = parada ordenada; ligar = retomar ou iniciar o serviço)",
)
async def set_bot_power(
    body: PowerRequest, principal: AdminDep, container: ContainerDep, audit: AuditDep, meta: MetaDep
) -> PowerOut:
    snapshot = await _alive(container)
    if not body.on:
        if not snapshot.is_on:
            raise Conflict("O robô já está desligado.")
        sent = await _send(
            CommandName.STOP,
            principal_name=principal.username,
            display=body.operator_name,
            reason=body.reason,
            container=container,
            audit=audit,
            meta=meta,
        )
        return PowerOut(state="commanded", message="Desligando o robô de forma ordenada.", command=sent)
    if snapshot.alive and snapshot.state not in {"STOPPED", "STOPPING"}:
        if snapshot.data and snapshot.data.get("halted"):
            raise Conflict("O robô está desligado por drawdown. Use Reativar.")
        if snapshot.data and snapshot.data.get("paused"):
            sent = await _send(
                CommandName.RESUME,
                principal_name=principal.username,
                display=body.operator_name,
                reason=body.reason,
                container=container,
                audit=audit,
                meta=meta,
            )
            return PowerOut(state="commanded", message="Retomando as operações.", command=sent)
        raise Conflict("O robô já está ligado.")
    state, message = await launch_runner(container)
    return PowerOut(state=state, message=message, command=None)


@router.get(
    "/control/commands",
    operation_id="listCommands",
    response_model=CommandPage,
    summary="Comandos enviados ao robô e o estado de cada um (pendente de entrega, aplicado...)",
)
async def list_commands(
    _: UserDep,
    container: ContainerDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: Annotated[str | None, Query(max_length=80)] = None,
) -> CommandPage:
    return await CommandService(container).list(limit=limit, cursor=cursor)


@router.get(
    "/control/commands/{command_id}",
    operation_id="getCommand",
    response_model=CommandOut,
    summary="Estado de um comando",
)
async def get_command(command_id: str, _: UserDep, container: ContainerDep) -> CommandOut:
    return await CommandService(container).get(command_id)


@router.post(
    "/live/activate",
    operation_id="activateLive",
    status_code=201,
    response_model=LiveAuthorizationOut,
    responses={
        409: PROBLEM_RESPONSES[409]
        | {"description": "Criterios nao atendidos: `errors` lista cada um com o motivo exato"}
    },
    summary="Autoriza o Live (grava a autorizacao append-only); NAO inicia o Live nem muda o modo",
)
async def activate_live(
    body: LiveActivationRequest,
    principal: FreshAdminDep,
    container: ContainerDep,
    audit: AuditDep,
    meta: MetaDep,
) -> LiveAuthorizationOut:
    return await LiveActivationService(container, audit).activate(principal=principal, body=body, meta=meta)


@router.post(
    "/live/authorization/revoke",
    operation_id="revokeLiveAuthorization",
    response_model=LiveAuthorizationOut,
    summary="Revoga a autorizacao do Live: o runner Live para novas entradas; os stops sao preservados",
)
async def revoke_live_authorization(
    body: LiveRevocationRequest,
    principal: FreshAdminDep,
    container: ContainerDep,
    audit: AuditDep,
    meta: MetaDep,
) -> LiveAuthorizationOut:
    return await LiveActivationService(container, audit).revoke(
        principal=principal, reason=body.reason, meta=meta
    )


@router.get(
    "/live/authorization",
    operation_id="getLiveAuthorization",
    response_model=LiveAuthorizationStateOut,
    summary="Autorizacao vigente do Live e a proxima etapa (capital reduzido, aumento de limites)",
)
async def get_live_authorization(
    _: UserDep, container: ContainerDep, audit: AuditDep
) -> LiveAuthorizationStateOut:
    return await LiveActivationService(container, audit).state()
