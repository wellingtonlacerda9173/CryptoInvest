import re
from collections.abc import Sequence
from datetime import datetime, timedelta

from engine.application.risk.state import AuditAction, AuditEntry, next_utc_midnight
from engine.application.selector import TradeSample

Span = tuple[datetime, datetime]

_UNTIL = re.compile(r"ate (\S+)$")


def _until(entry: AuditEntry) -> datetime:
    found = _UNTIL.search(entry.detail)
    return datetime.fromisoformat(found.group(1)) if found else next_utc_midnight(entry.at)


def lock_spans(events: Sequence[AuditEntry], end: datetime) -> list[Span]:
    """Intervalos em que o gate de risco bloqueou novas entradas (halt, pausa diaria, meta diaria, par)."""
    spans: list[Span] = []
    for event in events:
        match event.action:
            case AuditAction.HALTED | AuditAction.PAUSED:
                spans.append((event.at, end))
            case AuditAction.DAILY_LOSS_PAUSE | AuditAction.PAIR_PAUSED:
                spans.append((event.at, _until(event)))
            case AuditAction.DAILY_TARGET_REACHED:
                spans.append((event.at, next_utc_midnight(event.at)))
            case _:
                pass
    return spans


def _covered(spans: Sequence[Span], windows: Sequence[Span]) -> timedelta:
    total = timedelta()
    for w_start, w_end in windows:
        clipped = sorted((max(s, w_start), min(e, w_end)) for s, e in spans if s < w_end and e > w_start)
        cursor = w_start
        for s, e in clipped:
            s = max(s, cursor)
            if e > s:
                total += e - s
                cursor = e
    return total


def fraction_of(spans: Sequence[Span], windows: Sequence[Span]) -> float | None:
    """Fracao do tempo das janelas (fora da amostra) coberta pela uniao dos intervalos."""
    whole = sum((e - s for s, e in windows), timedelta())
    return _covered(spans, windows) / whole if whole else None


def exposure(samples: Sequence[TradeSample], windows: Sequence[Span]) -> float | None:
    return fraction_of([(s.opened_at, s.closed_at) for s in samples], windows)
