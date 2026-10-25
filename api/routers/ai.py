"""Uso do assistente de IA (gasto do mês, chamadas, falhas, circuito): leitura para leitor e admin."""

from typing import Annotated

from fastapi import APIRouter, Path, Query

from api.deps import AdminDep, AuditDep, ContainerDep, FreshAdminDep, MetaDep, UserDep
from api.errors import PROBLEM_RESPONSES
from api.schemas_ai import (
    AiValueOut,
    LessonDetailOut,
    LessonPage,
    LessonReviewRequest,
    LessonRunOut,
)
from api.schemas_trading import AiUsageOut
from api.services.ai_lessons import AiLessonService
from api.services.ai_usage import AiUsageService

LessonId = Annotated[str, Path(pattern=r"^[0-9a-f]{32}$", description="Id da lição")]

router = APIRouter(prefix="/ai", tags=["ai"], responses=PROBLEM_RESPONSES)


@router.get(
    "/usage",
    operation_id="getAiUsage",
    response_model=AiUsageOut,
    summary="Gasto estimado da IA no mês, chamadas, falhas e estado do circuito",
)
async def get_ai_usage(_: UserDep, container: ContainerDep) -> AiUsageOut:
    return await AiUsageService(container).build()


@router.get(
    "/lessons",
    operation_id="listAiLessons",
    response_model=LessonPage,
    summary="Lições da IA (mais recentes primeiro) com o estado de revisão",
)
async def list_ai_lessons(
    _: UserDep,
    container: ContainerDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: Annotated[str | None, Query(max_length=80)] = None,
) -> LessonPage:
    return await AiLessonService(container).list(limit=limit, cursor=cursor)


@router.post(
    "/lessons/run",
    operation_id="runAiLessons",
    response_model=LessonRunOut,
    status_code=202,
    summary="Gera um relatório de lições agora (em segundo plano, respeitando o orçamento)",
)
async def run_ai_lessons(
    principal: AdminDep, container: ContainerDep, audit: AuditDep, meta: MetaDep
) -> LessonRunOut:
    return await AiLessonService(container, audit).run(actor=principal.username, meta=meta)


@router.get(
    "/lessons/{lesson_id}",
    operation_id="getAiLesson",
    response_model=LessonDetailOut,
    summary="Uma lição com observações, evidência numérica e estatísticas do período",
)
async def get_ai_lesson(lesson_id: LessonId, _: UserDep, container: ContainerDep) -> LessonDetailOut:
    return await AiLessonService(container).detail(lesson_id)


@router.post(
    "/lessons/{lesson_id}/review",
    operation_id="reviewAiLesson",
    response_model=LessonDetailOut,
    summary="Aprova ou rejeita uma lição (admin com reautenticação; o motivo é obrigatório)",
)
async def review_ai_lesson(
    lesson_id: LessonId,
    body: LessonReviewRequest,
    principal: FreshAdminDep,
    container: ContainerDep,
    audit: AuditDep,
    meta: MetaDep,
) -> LessonDetailOut:
    return await AiLessonService(container, audit).review(
        lesson_id, decision=body.decision, reason=body.reason, actor=principal.username, meta=meta
    )


@router.get(
    "/value",
    operation_id="getAiValue",
    response_model=AiValueOut,
    summary="Valor da IA: o que os vetos teriam rendido, custo e um veredito honesto",
)
async def get_ai_value(
    _: UserDep, container: ContainerDep, days: Annotated[int, Query(ge=1, le=365)] = 30
) -> AiValueOut:
    return await AiLessonService(container).value(days)
