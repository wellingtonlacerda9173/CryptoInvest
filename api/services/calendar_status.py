"""Leitura SOMENTE LEITURA do calendario de eventos e do resultado da ultima tentativa de atualizacao."""

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import structlog

from api.container import Container
from api.schemas_calendar import CalendarEventOut, CalendarOut, CalendarState, CalendarUpdateOut, EventKind
from api.services.runner_status import RunnerStatusService
from engine.application.risk import EventCalendar, EventWindow
from engine.domain.risk import RiskConfig
from engine.infrastructure.persistence.config_repository import ConfigRepository
from engine.runner.calendar_store import EXPIRING_DAYS, read_calendar, read_status, resolve_calendar

log = structlog.get_logger()

UPCOMING = 10
RESULTS = {"updated", "unchanged", "rejected", "failed"}


def _update(status: dict[str, Any] | None) -> CalendarUpdateOut:
    if not status or status.get("result") not in RESULTS:
        return CalendarUpdateOut()
    try:
        attempted = datetime.fromisoformat(str(status["attempted_at"]))
    except (KeyError, ValueError):
        attempted = None
    return CalendarUpdateOut(attempted_at=attempted, result=status["result"])


_KINDS: tuple[tuple[str, EventKind, str], ...] = (
    ("FOMC", "fomc", "Federal Reserve (banco central dos EUA)"),
    ("EUA CPI", "cpi", "BLS (governo dos EUA)"),
    ("EUA payroll", "payroll", "BLS (governo dos EUA)"),
    ("Deribit", "deribit", "Deribit"),
)


def _event(window: EventWindow, before: timedelta, after: timedelta) -> CalendarEventOut:
    kind, source = next(
        ((k, src) for prefix, k, src in _KINDS if window.label.startswith(prefix)),
        ("other", "Calendario do robo"),
    )
    return CalendarEventOut(
        start=window.start,
        end=window.end,
        label=window.label,
        kind=kind,
        source=source,
        block_start=window.start - before,
        block_end=window.end + after,
    )


def _last_success(status: dict[str, Any] | None) -> datetime | None:
    if not status:
        return None
    ok = status.get("result") in ("updated", "unchanged")
    for key in ("last_success_at", *(("attempted_at",) if ok else ())):
        try:
            return datetime.fromisoformat(str(status[key]))
        except (KeyError, ValueError):
            continue
    return None


def build_calendar(
    path: Path | None,
    now: datetime,
    *,
    seed: Path | None = None,
    before_minutes: int = 30,
    after_minutes: int = 30,
) -> CalendarOut:
    """Mesma resolucao do robo (arquivo mutavel x semente da imagem): a tela nunca diverge do gate."""
    if path is not None:
        calendar = resolve_calendar(path, seed, now)
    else:
        calendar = read_calendar(seed) if seed is not None else None
    status = read_status(path) if path is not None else None
    calendar = calendar or EventCalendar()
    before, after = timedelta(minutes=before_minutes), timedelta(minutes=after_minutes)
    state: CalendarState = "expired"
    days: int | None = None
    if calendar.coverage_problem(now) is None and calendar.valid_until is not None:
        days = max(0, (calendar.valid_until - now).days)
        state = "expiring" if days < EXPIRING_DAYS else "ok"
    upcoming = sorted(
        (_event(w, before, after) for w in calendar.windows if w.end + after >= now),
        key=lambda e: (e.start, e.end),
    )
    return CalendarOut(
        state=state,
        now=now,
        last_success_at=_last_success(status),
        loaded_at=calendar.loaded_at,
        valid_until=calendar.valid_until,
        days_remaining=days,
        margin_before_minutes=before_minutes,
        margin_after_minutes=after_minutes,
        upcoming=upcoming[:UPCOMING],
        last_update=_update(status),
    )


async def _risk(container: Container) -> RiskConfig:
    try:
        raw = (await RunnerStatusService(container).snapshot()).data
        live = raw.get("risk") if raw else None
        if isinstance(live, dict):
            return RiskConfig.model_validate(live)
        async with container.sessions() as db:
            latest = await ConfigRepository(db).latest()
        return latest.document.risk if latest else RiskConfig()
    except Exception as error:
        log.warning("calendar_margins_unavailable", error=type(error).__name__)
        return RiskConfig()


async def read_calendar_status(container: Container, seed: Path | None) -> CalendarOut:
    risk = await _risk(container)
    return await asyncio.to_thread(
        build_calendar,
        container.settings.calendar_path,
        container.clock(),
        seed=seed,
        before_minutes=risk.event_margin_before_minutes,
        after_minutes=risk.event_margin_after_minutes,
    )
