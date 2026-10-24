"""Primeiro acesso: estado do assistente e a ação "Iniciar no Paper"."""

from fastapi import APIRouter

from api.deps import AdminDep, AuditDep, ContainerDep, MetaDep, UserDep
from api.errors import PROBLEM_RESPONSES
from api.schemas_control import OnboardingOut, StartPaperOut
from api.services.onboarding import OnboardingService

router = APIRouter(prefix="/onboarding", tags=["onboarding"], responses=PROBLEM_RESPONSES)


@router.get(
    "",
    operation_id="getOnboarding",
    response_model=OnboardingOut,
    summary="O que já foi feito no primeiro acesso (admin, 2FA, chave, risco, Paper)",
)
async def get_onboarding(_: UserDep, container: ContainerDep) -> OnboardingOut:
    return await OnboardingService(container).state()


@router.post(
    "/start-paper",
    operation_id="startPaper",
    response_model=StartPaperOut,
    status_code=202,
    summary="Inicia (ou retoma) o robô no Paper; nunca no Live",
)
async def start_paper(
    principal: AdminDep, container: ContainerDep, audit: AuditDep, meta: MetaDep
) -> StartPaperOut:
    return await OnboardingService(container, audit).start_paper(actor=principal.username, meta=meta)
