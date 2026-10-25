from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker

from api.app import create_app
from api.deps import require_user
from api.services.calendar_status import build_calendar
from engine.application.risk import EventCalendar, EventWindow
from engine.runner.calendar_store import serialize, write_status
from tests.api_support import make_settings

NOW = datetime(2026, 10, 25, 20, 0, tzinfo=UTC)


def stamp(day: int, month: int = 11, year: int = 2026) -> datetime:
    return datetime(year, month, day, 13, 30, tzinfo=UTC)


def write(path: Path, valid_until: datetime) -> None:
    windows = tuple(
        EventWindow(start=stamp(d), end=stamp(d).replace(hour=14), label=f"evento {d}") for d in range(1, 13)
    )
    calendar = EventCalendar(
        loaded_at=datetime(2026, 9, 1, tzinfo=UTC), valid_until=valid_until, windows=windows
    )
    path.write_text(serialize(calendar), encoding="utf-8")


def app_for(path: Path | None) -> FastAPI:
    return create_app(make_settings(calendar_path=path), sessions=async_sessionmaker())


async def get(app: FastAPI) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        return await client.get("/calendar")


async def test_requires_a_session(tmp_path: Path) -> None:
    write(tmp_path / "cal.json", datetime(2027, 1, 15, tzinfo=UTC))
    app = app_for(tmp_path / "cal.json")
    response = await get(app)
    assert response.status_code in {401, 403}


@pytest.mark.parametrize(
    ("valid_until", "state", "days"),
    [
        (datetime(2027, 1, 15, tzinfo=UTC), "ok", 81),
        (datetime(2026, 11, 5, tzinfo=UTC), "expiring", 10),
        (datetime(2026, 10, 1, tzinfo=UTC), "expired", None),
    ],
)
def test_state_and_days_remaining(
    tmp_path: Path, valid_until: datetime, state: str, days: int | None
) -> None:
    write(tmp_path / "cal.json", valid_until)
    out = build_calendar(tmp_path / "cal.json", NOW)
    assert (out.state, out.days_remaining) == (state, days)
    assert out.last_update.result == "never"


def test_upcoming_limited_sorted_and_status_reported(tmp_path: Path) -> None:
    path = tmp_path / "cal.json"
    write(path, datetime(2027, 1, 15, tzinfo=UTC))
    write_status(
        path,
        {
            "attempted_at": "2026-10-25T19:00:00Z",
            "result": "failed",
            "detail": "fonte indisponivel",
            "last_success_at": "2026-09-01T00:00:00Z",
        },
    )
    out = build_calendar(path, NOW)
    assert len(out.upcoming) == 10
    assert [e.start for e in out.upcoming] == sorted(e.start for e in out.upcoming)
    assert out.last_update.result == "failed"
    assert out.now == NOW and out.last_success_at == datetime(2026, 9, 1, tzinfo=UTC)
    assert {e.kind for e in out.upcoming} == {"other"}


def test_missing_invalid_or_unconfigured_is_expired(tmp_path: Path) -> None:
    assert build_calendar(None, NOW).state == "expired"
    assert build_calendar(tmp_path / "nao-existe.json", NOW).state == "expired"
    bad = tmp_path / "cal.json"
    bad.write_text("{quebrado", encoding="utf-8")
    assert build_calendar(bad, NOW).state == "expired"


async def test_endpoint_returns_payload_for_authenticated_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("api.routers.calendar.DEFAULT_CALENDAR", tmp_path / "sem-semente.json")
    write(tmp_path / "cal.json", datetime(2027, 1, 15, tzinfo=UTC))
    app = app_for(tmp_path / "cal.json")
    app.state.container.clock = lambda: NOW
    app.dependency_overrides[require_user] = lambda: cast(Any, object())
    response = await get(app)
    body = response.json()
    assert response.status_code == 200
    assert body["state"] == "ok"
    assert body["days_remaining"] == 81
    assert body["last_update"]["result"] == "never"
    assert body["upcoming"][0]["label"] == "evento 1"


def test_event_kind_and_source_from_label(tmp_path: Path) -> None:
    labels = [
        "FOMC decisao de juros (nov)",
        "EUA CPI (nov)",
        "EUA payroll (nov)",
        "Deribit vencimento mensal (nov)",
        "X",
    ]
    windows = tuple(
        EventWindow(start=stamp(d), end=stamp(d).replace(hour=14), label=label)
        for d, label in zip(range(1, 6), labels, strict=True)
    )
    path = tmp_path / "cal.json"
    path.write_text(
        serialize(
            EventCalendar(
                loaded_at=datetime(2026, 9, 1, tzinfo=UTC),
                valid_until=datetime(2027, 1, 1, tzinfo=UTC),
                windows=windows,
            )
        ),
        encoding="utf-8",
    )
    out = build_calendar(path, NOW)
    assert [e.kind for e in out.upcoming] == ["fomc", "cpi", "payroll", "deribit", "other"]
    assert all(e.source for e in out.upcoming)


def test_last_success_comes_from_status_not_from_loaded_at(tmp_path: Path) -> None:
    path = tmp_path / "cal.json"
    write(path, datetime(2027, 1, 15, tzinfo=UTC))
    write_status(path, {"attempted_at": "2026-10-25T19:00:00Z", "result": "unchanged"})

    assert build_calendar(path, NOW).last_success_at == datetime(2026, 10, 25, 19, 0, tzinfo=UTC)
    write_status(path, {"attempted_at": "2026-10-25T19:00:00Z", "result": "failed"})
    assert build_calendar(path, NOW).last_success_at is None


def test_events_expose_effective_block_with_margins_and_keep_events_in_post_margin(tmp_path: Path) -> None:
    path = tmp_path / "cal.json"
    event = EventWindow(start=NOW - timedelta(hours=2), end=NOW - timedelta(minutes=20), label="FOMC x")
    calendar = EventCalendar(
        loaded_at=NOW - timedelta(days=1), valid_until=NOW + timedelta(days=60), windows=(event,)
    )
    path.write_text(serialize(calendar), encoding="utf-8")

    out = build_calendar(path, NOW, before_minutes=30, after_minutes=30)

    (shown,) = out.upcoming
    assert shown.block_start == event.start - timedelta(minutes=30)
    assert shown.block_end == event.end + timedelta(minutes=30) and shown.block_end >= NOW
    assert out.margin_before_minutes == 30 and out.margin_after_minutes == 30
    assert build_calendar(path, NOW + timedelta(minutes=11)).upcoming == []


def test_without_calendar_path_resolves_with_seed_only(tmp_path: Path) -> None:
    seed = tmp_path / "seed.json"
    write(seed, datetime(2027, 1, 15, tzinfo=UTC))
    out = build_calendar(None, NOW, seed=seed)
    assert (out.state, out.days_remaining) == ("ok", 81)
    assert out.last_update.result == "never"
    assert build_calendar(None, NOW, seed=tmp_path / "nao-existe.json").state == "expired"
