"""O config/volatility_events.json real: carrega, e coerente e respeita a cobertura declarada."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from engine.application.risk import EventCalendar

CALENDAR_PATH = Path(__file__).resolve().parents[3] / "config" / "volatility_events.json"
CALENDAR = EventCalendar.load(CALENDAR_PATH)


def test_declares_coverage_and_has_windows() -> None:
    assert CALENDAR.loaded_at is not None
    assert CALENDAR.valid_until is not None
    assert CALENDAR.windows
    assert CALENDAR.coverage_problem(CALENDAR.loaded_at) is None


def test_windows_sorted_non_overlapping_and_inside_coverage() -> None:
    starts = [w.start for w in CALENDAR.windows]
    assert starts == sorted(starts)
    assert all(a.end < b.start for a, b in zip(CALENDAR.windows, CALENDAR.windows[1:], strict=False))
    assert CALENDAR.loaded_at is not None
    assert CALENDAR.valid_until is not None
    assert all(CALENDAR.loaded_at <= w.start and w.end <= CALENDAR.valid_until for w in CALENDAR.windows)


def test_fail_closed_after_valid_until() -> None:
    assert CALENDAR.valid_until is not None
    assert CALENDAR.coverage_problem(CALENDAR.valid_until + timedelta(seconds=1)) is not None


def test_fomc_window_blocks_entries() -> None:
    margin = timedelta(minutes=30)
    inside = datetime(2026, 10, 28, 17, 45, tzinfo=UTC)
    assert CALENDAR.active_at(inside, margin, margin) is not None
    assert CALENDAR.active_at(datetime(2026, 10, 28, 15, 0, tzinfo=UTC), margin, margin) is None
