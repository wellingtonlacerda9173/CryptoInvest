from fastapi import APIRouter, Response
from sqlalchemy import text

from api.deps import ContainerDep
from api.schemas import HealthOut, ReadyOut

router = APIRouter(tags=["health"])


@router.get("/health", operation_id="getHealth", response_model=HealthOut, summary="Processo no ar")
async def health() -> HealthOut:
    return HealthOut()


@router.get(
    "/ready",
    operation_id="getReadiness",
    response_model=ReadyOut,
    summary="Pronto para receber trafego (banco acessivel)",
    responses={503: {"model": ReadyOut, "description": "Dependencia indisponivel"}},
)
async def ready(container: ContainerDep, response: Response) -> ReadyOut:
    try:
        async with container.sessions() as db:
            await db.execute(text("SELECT 1"))
    except Exception:
        response.status_code = 503
        return ReadyOut(status="unavailable", database=False)
    return ReadyOut(status="ready", database=True)
