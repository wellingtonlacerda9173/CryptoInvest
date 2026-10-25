"""Persistencia e regras de seguranca do calendario de eventos (fail-closed).

O calendario so pode CRESCER: cobertura (`valid_until`) e janelas nunca regridem, nem no atualizador nem no
recarregamento do runner. Arquivo invalido, ilegivel ou que regrida e ignorado e o anterior permanece.
"""

import contextlib
import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog
from pydantic import ValidationError

from engine.application.risk import EventCalendar, EventWindow

log = structlog.get_logger()

MAX_WINDOWS = 500
MAX_ADDED_PER_RUN = 80
MAX_WINDOW_HOURS = 6
MAX_HORIZON_DAYS = 800
MAX_FILE_BYTES = 512 * 1024
EXPIRING_DAYS = 14
STATUS_SUFFIX = ".status.json"
AUDIT_SUFFIX = ".audit.jsonl"
BACKUP_SUFFIX = ".bak"
LOADED_AT_TOLERANCE = timedelta(minutes=5)


def read_calendar(path: Path) -> EventCalendar | None:
    """None se ausente/ilegivel/invalido: nunca levanta."""
    try:
        if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
            return None
        return EventCalendar.load(path)
    except (OSError, ValueError, ValidationError) as error:
        log.error("event_calendar_unreadable", path=str(path), error=type(error).__name__)
        return None


def _strength(calendar: EventCalendar, now: datetime) -> tuple[datetime, int]:
    future = sum(1 for w in calendar.windows if w.end > now)
    return calendar.valid_until or datetime.min.replace(tzinfo=UTC), future


def readable_calendars(path: Path, seed: Path | None = None) -> list[EventCalendar]:
    candidates = (read_calendar(path), read_calendar(seed) if seed and seed != path else None)
    return [c for c in candidates if c]


def resolve_calendar(
    path: Path, seed: Path | None = None, now: datetime | None = None
) -> EventCalendar | None:
    """Melhor entre o mutavel e a semente: maior cobertura; empate: mais janelas futuras, depois o mutavel."""
    found = readable_calendars(path, seed)
    if not found:
        return None
    reference = now or datetime.now(UTC)
    return max(found, key=lambda c: _strength(c, reference))


def _covers(new: EventWindow, old: EventWindow) -> bool:
    return new.start <= old.start and new.end >= old.end


def regression_problem(old: EventCalendar, new: EventCalendar, now: datetime | None = None) -> str | None:
    """Motivo pelo qual `new` enfraquece `old`; None se so adiciona/estende. `now` ignora janelas passadas."""
    if old.valid_until is not None and (new.valid_until is None or new.valid_until < old.valid_until):
        return "valid_until regrediu"
    if (
        old.loaded_at is not None
        and old.loaded_at <= datetime.now(UTC) + LOADED_AT_TOLERANCE
        and (new.loaded_at is None or new.loaded_at < old.loaded_at)
    ):
        return "loaded_at regrediu"
    if old.acknowledged_no_events and not new.acknowledged_no_events and not new.windows:
        return "confirmacao de calendario sem eventos removida"
    for window in old.windows:
        if now is not None and window.end < now:
            continue
        if not any(_covers(candidate, window) for candidate in new.windows):
            return f"janela existente removida ou encurtada: {window.label} {window.start.isoformat()}"
    return None


def sanity_problem(calendar: EventCalendar, previous: EventCalendar, now: datetime) -> str | None:
    if calendar.loaded_at is None or calendar.valid_until is None:
        return "cobertura ausente"
    if calendar.loaded_at > now:
        return "loaded_at no futuro"
    if calendar.valid_until <= now:
        return "valid_until nao e futuro"
    if calendar.valid_until > now + timedelta(days=MAX_HORIZON_DAYS):
        return "valid_until alem do horizonte maximo"
    if len(calendar.windows) > MAX_WINDOWS:
        return "janelas acima do limite"
    if len(calendar.windows) - len(previous.windows) > MAX_ADDED_PER_RUN:
        return "janelas adicionadas acima do limite por execucao"
    starts = [w.start for w in calendar.windows]
    if starts != sorted(starts):
        return "janelas fora de ordem"
    keys = [(w.start, w.end) for w in calendar.windows]
    if len(keys) != len(set(keys)):
        return "janelas duplicadas"
    known = {(w.start, w.end) for w in previous.windows}
    for window in calendar.windows:
        if (window.start, window.end) not in known and window.end - window.start > timedelta(
            hours=MAX_WINDOW_HOURS
        ):
            return f"janela nova longa demais: {window.label}"
    return None


def merge_windows(
    existing: tuple[EventWindow, ...], candidates: tuple[EventWindow, ...], now: datetime
) -> tuple[EventWindow, ...]:
    """So adiciona/estende: candidato futuro que sobrepoe janela existente a alarga (uniao); senao entra."""
    merged = list(existing)
    for candidate in candidates:
        if candidate.end <= now:
            continue
        for index, current in enumerate(merged):
            if candidate.start <= current.end and current.start <= candidate.end:
                merged[index] = EventWindow(
                    start=min(current.start, candidate.start),
                    end=max(current.end, candidate.end),
                    label=current.label,
                )
                break
        else:
            merged.append(candidate)
    return tuple(sorted(merged, key=lambda w: (w.start, w.end)))


def serialize(calendar: EventCalendar) -> str:
    def stamp(moment: datetime | None) -> str | None:
        return None if moment is None else moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    lines = [
        f'    {{"start": "{stamp(w.start)}", "end": "{stamp(w.end)}", '
        f'"label": {json.dumps(w.label, ensure_ascii=False)}}}'
        for w in calendar.windows
    ]
    body = ",\n".join(lines)
    return (
        "{\n"
        f'  "loaded_at": {json.dumps(stamp(calendar.loaded_at))},\n'
        f'  "valid_until": {json.dumps(stamp(calendar.valid_until))},\n'
        f'  "acknowledged_no_events": {json.dumps(calendar.acknowledged_no_events)},\n'
        f'  "windows": [\n{body}\n  ]\n'
        "}\n"
    )


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp_name)
        raise


def sibling(path: Path, suffix: str) -> Path:
    return path.with_name(path.name + suffix)


def write_status(path: Path, payload: dict[str, Any]) -> None:
    with contextlib.suppress(OSError):
        atomic_write(sibling(path, STATUS_SUFFIX), json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def append_audit(path: Path, payload: dict[str, Any]) -> None:
    with contextlib.suppress(OSError):
        target = sibling(path, AUDIT_SUFFIX)
        with target.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False) + "\n")


def read_status(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(sibling(path, STATUS_SUFFIX).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


class CalendarReloader:
    """Recarga segura do runner: so troca o calendario em memoria por arquivo valido e sem regressao."""

    def __init__(self, path: Path, seed: Path | None = None) -> None:
        self.path = path
        self.seed = seed

    def reload(self, current: EventCalendar, now: datetime) -> EventCalendar:
        fresh = resolve_calendar(self.path, self.seed)
        if fresh is None:
            log.warning("event_calendar_reload_ignored", reason="arquivo ausente ou invalido", kept=True)
            return current
        if fresh == current:
            return current
        problem = regression_problem(current, fresh, now)
        if problem is not None:
            log.warning("event_calendar_reload_ignored", reason=problem, kept=True)
            return current
        log.info(
            "event_calendar_reloaded",
            valid_until=fresh.valid_until.isoformat() if fresh.valid_until else None,
            windows=len(fresh.windows),
        )
        return fresh
