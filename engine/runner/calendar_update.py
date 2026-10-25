"""Atualizador automatico do calendario de eventos de volatilidade (so fontes oficiais publicas, sem chave).

    python -m engine.runner.calendar_update --calendar <arquivo> [--seed <semente>] [--dry-run]
    python -m engine.runner.calendar_update --calendar <arquivo> --loop     # servico: 1 execucao a cada 24 h

Fail-closed: qualquer fonte com erro => nada muda. `valid_until` so avanca ate o MINIMO das coberturas
confirmadas; janelas so sao adicionadas/estendidas; o candidato passa pelo schema `EventCalendar`, por
checagens de sanidade e por `regression_problem` antes de uma escrita atomica (backup `.bak`, trilha
`.audit.jsonl` e estado `.status.json` ao lado do arquivo).
"""

import argparse
import asyncio
import contextlib
import shutil
import signal
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import structlog
from pydantic import ValidationError

from engine.application.risk import EventCalendar
from engine.infrastructure.config.logging import configure_logging
from engine.runner.calendar_sources import MAX_CLOCK_DRIFT, Fetcher, HttpsFetcher, collect_sources
from engine.runner.calendar_store import (
    BACKUP_SUFFIX,
    append_audit,
    atomic_write,
    digest,
    merge_windows,
    read_calendar,
    read_status,
    readable_calendars,
    regression_problem,
    resolve_calendar,
    sanity_problem,
    serialize,
    sibling,
    write_status,
)

log = structlog.get_logger()

DEFAULT_CALENDAR = Path("config/volatility_events.json")
LOOP_SECONDS = 24 * 3600
RETRY_SECONDS = 2 * 3600

Result = Literal["updated", "unchanged", "rejected", "failed"]


@dataclass(frozen=True, slots=True)
class UpdateReport:
    result: Result
    detail: str
    valid_until: datetime | None = None
    added: tuple[str, ...] = ()
    sources: dict[str, str] = field(default_factory=dict)
    dry_run: bool = False


def _stamp(moment: datetime | None) -> str | None:
    return None if moment is None else moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _finish(path: Path, now: datetime, report: UpdateReport, *, audit: bool = False) -> UpdateReport:
    payload: dict[str, Any] = {
        "attempted_at": _stamp(now),
        "result": report.result,
        "detail": report.detail,
        "valid_until": _stamp(report.valid_until),
        "added": list(report.added),
        "sources": report.sources,
        "dry_run": report.dry_run,
    }
    succeeded = report.result in ("updated", "unchanged")
    previous = read_status(path) or {}
    payload["last_success_at"] = _stamp(now) if succeeded else previous.get("last_success_at")
    level = log.info if succeeded else log.error
    level("event_calendar_update", **payload)
    if not report.dry_run:
        write_status(path, payload)
        if audit:
            append_audit(path, payload)
    return report


def _unreadable(path: Path) -> bool:
    return path.exists() and read_calendar(path) is None


def _commit(path: Path, text: str, candidate: EventCalendar) -> bool:
    backup = sibling(path, BACKUP_SUFFIX)
    had_previous = path.is_file()
    if had_previous:
        shutil.copy2(path, backup)
    atomic_write(path, text)
    if read_calendar(path) == candidate:
        return True
    if had_previous and backup.is_file():
        atomic_write(path, backup.read_text(encoding="utf-8"))
    else:
        with contextlib.suppress(OSError):
            path.unlink()
    return False


async def run_update(
    path: Path, fetcher: Fetcher, now: datetime, *, seed: Path | None = None, dry_run: bool = False
) -> UpdateReport:
    now = now.replace(microsecond=0)
    if _unreadable(path):
        return _finish(
            path,
            now,
            UpdateReport("failed", "arquivo atual ilegivel ou invalido: nada alterado", dry_run=dry_run),
        )
    current = resolve_calendar(path, seed, now) or EventCalendar()
    try:
        ok, errors = await collect_sources(fetcher, now)
    except Exception as error:
        ok, errors = [], {"coleta": f"erro inesperado: {type(error).__name__}"}
    if errors or not ok:
        return _finish(
            path,
            now,
            UpdateReport(
                "failed", "fonte indisponivel ou ambigua: cobertura mantida", sources=errors, dry_run=dry_run
            ),
        )
    drift = getattr(fetcher, "clock_drift", lambda: None)()
    if drift is not None and drift > MAX_CLOCK_DRIFT:
        return _finish(
            path,
            now,
            UpdateReport(
                "failed",
                f"relogio do host diverge do servidor em {int(drift.total_seconds())} s: nada alterado",
                dry_run=dry_run,
            ),
        )
    server_now = getattr(fetcher, "server_now", lambda: None)()
    loaded_at = min(now, server_now.replace(microsecond=0)) if server_now is not None else now
    sources = {s.name: _stamp(s.covered_until) or "" for s in ok}
    covered = min(s.covered_until for s in ok)
    valid_until = max(filter(None, (current.valid_until, covered)))
    windows = merge_windows(current.windows, tuple(w for s in ok for w in s.windows), now)
    added = tuple(
        f"{w.label} {_stamp(w.start)}"
        for w in windows
        if (w.start, w.end) not in {(x.start, x.end) for x in current.windows}
    )
    if valid_until == current.valid_until and windows == current.windows:
        return _finish(
            path,
            now,
            UpdateReport(
                "unchanged", "sem novidades confirmadas", valid_until, sources=sources, dry_run=dry_run
            ),
        )
    try:
        candidate = EventCalendar.model_validate(
            {
                "loaded_at": loaded_at,
                "valid_until": valid_until,
                "acknowledged_no_events": current.acknowledged_no_events,
                "windows": [w.model_dump() for w in windows],
            }
        )
    except ValidationError as error:
        return _finish(
            path,
            now,
            UpdateReport(
                "rejected", f"schema: {error.error_count()} erro(s)", sources=sources, dry_run=dry_run
            ),
        )
    problem = sanity_problem(candidate, current, now)
    for base in readable_calendars(path, seed):
        problem = problem or regression_problem(base, candidate, now)
    if problem is not None:
        return _finish(path, now, UpdateReport("rejected", problem, sources=sources, dry_run=dry_run))
    text = serialize(candidate)
    if EventCalendar.from_json(text) != candidate:
        return _finish(
            path,
            now,
            UpdateReport(
                "rejected", "serializacao nao reproduz o candidato", sources=sources, dry_run=dry_run
            ),
        )
    report = UpdateReport("updated", f"sha256={digest(text)}", candidate.valid_until, added, sources, dry_run)
    if dry_run:
        return _finish(path, now, report)
    if not _commit(path, text, candidate):
        return _finish(
            path, now, UpdateReport("failed", "releitura divergente: backup restaurado", sources=sources)
        )
    return _finish(path, now, report, audit=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="engine.runner.calendar_update", description=__doc__)
    parser.add_argument("--calendar", type=Path, default=DEFAULT_CALENDAR)
    parser.add_argument(
        "--seed", type=Path, default=None, help="arquivo base (imagem) usado se o mutavel nao existe"
    )
    parser.add_argument("--dry-run", action="store_true", help="busca e valida, mas nao escreve nada")
    parser.add_argument("--loop", action="store_true", help="servico: repete a cada 24 h (2 h apos falha)")
    parser.add_argument("--json-logs", action="store_true")
    return parser


async def serve(args: argparse.Namespace) -> int:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop.set)
    while True:
        try:
            report = await run_update(
                args.calendar, HttpsFetcher(), datetime.now(UTC), seed=args.seed, dry_run=args.dry_run
            )
        except Exception as error:
            log.error("event_calendar_update_crashed", error=type(error).__name__)
            report = UpdateReport("failed", f"erro inesperado: {type(error).__name__}")
        if not args.loop:
            return 0 if report.result in ("updated", "unchanged") else 1
        wait = LOOP_SECONDS if report.result in ("updated", "unchanged") else RETRY_SECONDS
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), wait)
        if stop.is_set():
            return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(json=args.json_logs)
    return asyncio.run(serve(args))


if __name__ == "__main__":
    sys.exit(main())
