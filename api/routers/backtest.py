"""Backtests: o leitor acompanha e lê; só o admin cria e cancela. A execução é em subprocesso."""

from typing import Annotated

from fastapi import APIRouter, Path, Query

from api.deps import AdminDep, AuditDep, ContainerDep, MetaDep, UserDep
from api.errors import PROBLEM_RESPONSES
from api.schemas_backtest import BacktestCreate, BacktestJobOut, BacktestPage, BacktestReportOut
from api.services.backtests import BacktestService, job_out

router = APIRouter(prefix="/backtests", tags=["backtest"], responses=PROBLEM_RESPONSES)

JobId = Annotated[str, Path(pattern=r"^[0-9a-f]{32}$", description="Id do backtest")]


@router.post(
    "",
    operation_id="createBacktest",
    response_model=BacktestJobOut,
    status_code=202,
    summary="Cria um backtest (roda fora da API; acompanhe pelo progresso)",
)
async def create_backtest(
    body: BacktestCreate, principal: AdminDep, container: ContainerDep, audit: AuditDep, meta: MetaDep
) -> BacktestJobOut:
    return await BacktestService(container, audit).create(body, actor=principal.username, meta=meta)


@router.get(
    "",
    operation_id="listBacktests",
    response_model=BacktestPage,
    summary="Backtests recentes e o estado de cada um",
)
async def list_backtests(
    _: UserDep,
    container: ContainerDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: Annotated[str | None, Query(max_length=80)] = None,
) -> BacktestPage:
    return await BacktestService(container).list(limit=limit, cursor=cursor)


@router.get(
    "/{job_id}",
    operation_id="getBacktest",
    response_model=BacktestJobOut,
    summary="Estado e progresso de um backtest",
)
async def get_backtest(job_id: JobId, _: UserDep, container: ContainerDep) -> BacktestJobOut:
    return job_out(await BacktestService(container).get(job_id))


@router.get(
    "/{job_id}/report",
    operation_id="getBacktestReport",
    response_model=BacktestReportOut,
    summary="Relatório final: métricas, curva, operações, evitadas e walk-forward",
)
async def get_backtest_report(job_id: JobId, _: UserDep, container: ContainerDep) -> BacktestReportOut:
    return await BacktestService(container).report(job_id)


@router.post(
    "/{job_id}/cancel",
    operation_id="cancelBacktest",
    response_model=BacktestJobOut,
    summary="Cancela um backtest em fila ou em execução",
)
async def cancel_backtest(
    job_id: JobId, principal: AdminDep, container: ContainerDep, audit: AuditDep, meta: MetaDep
) -> BacktestJobOut:
    return await BacktestService(container, audit).cancel(job_id, actor=principal.username, meta=meta)
