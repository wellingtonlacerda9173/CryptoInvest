import asyncio
import importlib.util
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from engine.application.risk import EventCalendar, EventWindow
from engine.runner import calendar_store
from engine.runner.calendar_sources import CalendarSourceError, collect_sources, parse_deribit
from engine.runner.calendar_store import resolve_calendar, serialize, sibling
from engine.runner.calendar_update import run_update
from tests.unit.runner.test_calendar_update import NOW, FakeFetcher, seed_file

HEALTHCHECK = Path(__file__).resolve().parents[3] / "deploy" / "healthcheck.py"
_spec = importlib.util.spec_from_file_location("healthcheck", HEALTHCHECK)
assert _spec and _spec.loader
healthcheck = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(healthcheck)


class DriftedFetcher(FakeFetcher):
    def clock_drift(self) -> timedelta:
        return timedelta(hours=3)


def stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_healthcheck_fails_when_every_recent_run_fails_and_coverage_is_short() -> None:
    now = NOW.timestamp()
    status = {"result": "failed", "attempted_at": stamp(NOW)}
    assert not healthcheck.calendar_health(status, stamp(NOW + timedelta(days=3)), now)


def test_healthcheck_accepts_recent_success_or_long_coverage() -> None:
    now = NOW.timestamp()
    assert healthcheck.calendar_health(
        {"result": "unchanged", "attempted_at": stamp(NOW - timedelta(hours=2))}, None, now
    )
    assert not healthcheck.calendar_health(
        {"result": "unchanged", "attempted_at": stamp(NOW - timedelta(days=3))}, None, now
    )
    assert healthcheck.calendar_health({"result": "failed"}, stamp(NOW + timedelta(days=8)), now)


def test_year_zero_and_far_future_deribit_timestamps_become_source_errors() -> None:
    bad = json.dumps(
        {"result": [{"kind": "option", "base_currency": "BTC", "expiration_timestamp": 99999999999999999}]}
    )
    with pytest.raises(CalendarSourceError):
        parse_deribit({"BTC": bad, "ETH": bad.replace("BTC", "ETH")}, NOW)


def test_unexpected_parser_exception_is_a_source_failure_without_leaking_message() -> None:
    class Exploding(FakeFetcher):
        async def get(self, url: str) -> str:
            raise RuntimeError("segredo interno")

    ok, errors = asyncio.run(collect_sources(Exploding(), NOW))
    assert not ok and set(errors) == {"fomc", "empsit", "cpi", "deribit"}
    assert all("segredo" not in message for message in errors.values())


async def test_host_clock_drift_rejects_the_round_and_writes_nothing(tmp_path: Path) -> None:
    path = tmp_path / "cal.json"
    seed_file(path)
    before = path.read_text(encoding="utf-8")
    report = await run_update(path, DriftedFetcher(), NOW)
    assert report.result == "failed" and "relogio" in report.detail
    assert path.read_text(encoding="utf-8") == before


async def test_last_success_survives_a_later_failure(tmp_path: Path) -> None:
    path = tmp_path / "cal.json"
    seed_file(path)
    assert (await run_update(path, FakeFetcher(), NOW)).result == "updated"
    later = NOW + timedelta(hours=2)
    assert (await run_update(path, FakeFetcher(cpi=RuntimeError("x")), later)).result == "failed"
    status = json.loads(sibling(path, ".status.json").read_text(encoding="utf-8"))
    assert status["last_success_at"] == stamp(NOW)


def test_resolve_tie_prefers_calendar_with_more_future_windows(tmp_path: Path) -> None:
    until = NOW + timedelta(days=60)

    def build(count: int) -> EventCalendar:
        windows = tuple(
            EventWindow(start=NOW + timedelta(days=d), end=NOW + timedelta(days=d, hours=1), label="x")
            for d in range(1, count + 1)
        )
        return EventCalendar(loaded_at=NOW - timedelta(days=1), valid_until=until, windows=windows)

    volume, seed = tmp_path / "v.json", tmp_path / "s.json"
    volume.write_text(serialize(build(1)), encoding="utf-8")
    seed.write_text(serialize(build(3)), encoding="utf-8")
    resolved = resolve_calendar(volume, seed, NOW)
    assert resolved is not None and len(resolved.windows) == 3


async def test_failed_reread_without_backup_removes_the_broken_file(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "new.json"
    monkeypatch.setattr(calendar_store, "read_calendar", lambda _p: None)
    from engine.runner import calendar_update

    monkeypatch.setattr(calendar_update, "read_calendar", lambda _p: None)
    candidate = EventCalendar(loaded_at=NOW, valid_until=NOW + timedelta(days=60), windows=())
    assert calendar_update._commit(path, serialize(candidate), candidate) is False
    assert not path.exists()
