"""Calendario de eventos de volatilidade: leitura para qualquer usuario autenticado; nada aqui escreve."""

from fastapi import APIRouter

from api.deps import ContainerDep, UserDep
from api.errors import PROBLEM_RESPONSES
from api.schemas_calendar import CalendarOut
from api.services.calendar_status import read_calendar_status
from engine.runner.calendar_update import DEFAULT_CALENDAR

router = APIRouter(tags=["calendar"], responses=PROBLEM_RESPONSES)


@router.get(
    "/calendar",
    operation_id="getEventCalendar",
    response_model=CalendarOut,
    summary="Calendario de eventos: proximas janelas, cobertura e ultima atualizacao automatica",
)
async def get_event_calendar(_: UserDep, container: ContainerDep) -> CalendarOut:
    return await read_calendar_status(container, DEFAULT_CALENDAR)
