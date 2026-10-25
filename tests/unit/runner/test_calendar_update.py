import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from engine.application.risk import EventCalendar, EventWindow
from engine.runner import calendar_store
from engine.runner.calendar_sources import (
    CPI_URL,
    DERIBIT_URL,
    EMPSIT_URL,
    FOMC_URL,
    CalendarSourceError,
    parse_bls,
    parse_deribit,
    parse_fomc,
)
from engine.runner.calendar_store import (
    CalendarReloader,
    merge_windows,
    read_calendar,
    read_status,
    regression_problem,
    sibling,
)
from engine.runner.calendar_update import run_update

FIX = Path(__file__).resolve().parents[2] / "fixtures" / "calendar"
NOW = datetime(2026, 10, 25, 20, 0, tzinfo=UTC)
MANUAL = Path(__file__).resolve().parents[3] / "config" / "volatility_events.json"
URLS = {"fomc": FOMC_URL, "cpi": CPI_URL, "empsit": EMPSIT_URL}


def text(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


class FakeFetcher:
    def __init__(self, **overrides: str | Exception) -> None:
        self.pages: dict[str, str | Exception] = {
            FOMC_URL: text("fomc.html"),
            EMPSIT_URL: text("empsit.html"),
            CPI_URL: text("cpi.html"),
            DERIBIT_URL.format(currency="BTC"): text("deribit_btc.json"),
            DERIBIT_URL.format(currency="ETH"): text("deribit_eth.json"),
        }
        self.pages.update({URLS[key]: value for key, value in overrides.items()})

    async def get(self, url: str) -> str:
        page = self.pages[url]
        if isinstance(page, Exception):
            raise page
        return page


def window(start: str, end: str, label: str = "x") -> EventWindow:
    return EventWindow(start=datetime.fromisoformat(start), end=datetime.fromisoformat(end), label=label)


def calendar(
    valid_until: str, windows: tuple[EventWindow, ...] = (), loaded_at: str = "2026-10-01T00:00:00+00:00"
) -> EventCalendar:
    return EventCalendar(
        loaded_at=datetime.fromisoformat(loaded_at),
        valid_until=datetime.fromisoformat(valid_until),
        windows=windows,
    )


def seed_file(path: Path, valid_until: str = "2026-11-30T23:59:59+00:00") -> None:
    base = calendar(
        valid_until, (window("2026-10-28T18:00:00+00:00", "2026-10-28T19:30:00+00:00", "FOMC manual"),)
    )
    path.write_text(calendar_store.serialize(base), encoding="utf-8")


def test_fomc_uses_new_york_wall_clock_across_dst() -> None:
    result = parse_fomc(text("fomc.html"), NOW)
    starts = {w.start.isoformat(): w for w in result.windows}
    assert "2026-10-28T18:00:00+00:00" in starts
    assert "2026-12-09T19:00:00+00:00" in starts
    assert "2027-03-17T18:00:00+00:00" in starts
    assert starts["2026-12-09T19:00:00+00:00"].end.isoformat() == "2026-12-09T20:30:00+00:00"
    assert result.covered_until == datetime(2027, 12, 31, 23, 59, 59, tzinfo=UTC)
    assert all(w.end > NOW for w in result.windows)


@pytest.mark.parametrize(("name", "page"), [("empsit", "empsit.html"), ("cpi", "cpi.html")])
def test_bls_release_at_0830_et(name: str, page: str) -> None:
    result = parse_bls(text(page), name, "x", NOW)
    winter = [w for w in result.windows if w.start < datetime(2027, 3, 1, tzinfo=UTC)]
    summer = [w for w in result.windows if w.start.month in (5, 6, 7, 8)]
    assert winter
    assert summer
    assert {(w.start.hour, w.start.minute) for w in winter} == {(13, 30)}
    assert {(w.start.hour, w.start.minute) for w in summer} == {(12, 30)}
    assert result.covered_until.hour == 23


def test_bls_known_release() -> None:
    result = parse_bls(text("empsit.html"), "empsit", "payroll", NOW)
    assert result.windows[0].start.isoformat() == "2026-11-06T13:30:00+00:00"
    assert result.windows[0].end.isoformat() == "2026-11-06T14:30:00+00:00"


def test_deribit_coverage_stops_at_first_missing_month() -> None:
    result = parse_deribit({"BTC": text("deribit_btc.json"), "ETH": text("deribit_eth.json")}, NOW)
    assert [w.start.isoformat() for w in result.windows] == [
        "2026-10-30T07:00:00+00:00",
        "2026-11-27T07:00:00+00:00",
        "2026-12-25T07:00:00+00:00",
    ]
    assert result.covered_until == datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC)
    assert "trimestral" in result.windows[-1].label


def test_deribit_requires_both_currencies() -> None:
    with pytest.raises(CalendarSourceError):
        parse_deribit({"BTC": text("deribit_btc.json")}, NOW)
    with pytest.raises(CalendarSourceError):
        parse_deribit({"BTC": text("deribit_btc.json"), "ETH": "{}"}, NOW)


def test_ambiguous_or_changed_pages_are_refused() -> None:
    with pytest.raises(CalendarSourceError):
        parse_fomc(text("fomc.html").replace("27-28", "27-28 (tentativo)", 1), NOW)
    with pytest.raises(CalendarSourceError):
        parse_fomc("<html>nova estrutura</html>", NOW)
    with pytest.raises(CalendarSourceError):
        parse_bls(text("cpi.html").replace("Release Time", "Hora"), "cpi", "CPI", NOW)
    with pytest.raises(CalendarSourceError):
        parse_bls(text("cpi.html").replace("08:30 AM", "10:00 AM", 1), "cpi", "CPI", NOW)
    with pytest.raises(CalendarSourceError):
        parse_bls(text("cpi.html").replace("Dec. 10, 2026", "Dec. 10, 2025", 1), "cpi", "CPI", NOW)


def test_regression_rules() -> None:
    old = calendar(
        "2027-01-15T23:59:59+00:00", (window("2026-11-06T13:30:00+00:00", "2026-11-06T14:30:00+00:00"),)
    )
    longer = calendar(
        "2027-02-01T00:00:00+00:00",
        (window("2026-11-06T13:00:00+00:00", "2026-11-06T15:00:00+00:00"),),
        "2026-10-02T00:00:00+00:00",
    )
    assert regression_problem(old, longer) is None
    assert regression_problem(old, calendar("2027-01-01T00:00:00+00:00", old.windows))
    assert regression_problem(old, calendar("2027-02-01T00:00:00+00:00", ()))
    shorter = (window("2026-11-06T13:30:00+00:00", "2026-11-06T14:00:00+00:00"),)
    assert regression_problem(old, calendar("2027-02-01T00:00:00+00:00", shorter))
    assert regression_problem(
        old, calendar("2027-02-01T00:00:00+00:00", old.windows, "2026-09-01T00:00:00+00:00")
    )
    past = datetime(2026, 12, 1, tzinfo=UTC)
    assert regression_problem(old, calendar("2027-02-01T00:00:00+00:00", ()), now=past) is None


def test_merge_never_shortens_and_extends_by_union() -> None:
    wide = window("2026-11-06T13:00:00+00:00", "2026-11-06T16:00:00+00:00", "largo")
    merged = merge_windows((wide,), (window("2026-11-06T13:30:00+00:00", "2026-11-06T14:30:00+00:00"),), NOW)
    assert merged == (wide,)
    narrow = window("2026-11-06T13:30:00+00:00", "2026-11-06T14:00:00+00:00", "a")
    ext = merge_windows((narrow,), (window("2026-11-06T13:30:00+00:00", "2026-11-06T14:30:00+00:00"),), NOW)
    assert ext[0].end.isoformat() == "2026-11-06T14:30:00+00:00"
    assert ext[0].label == "a"
    assert merge_windows((), (window("2026-10-01T00:00:00+00:00", "2026-10-01T01:00:00+00:00"),), NOW) == ()


async def test_update_extends_coverage_to_minimum_of_sources(tmp_path: Path) -> None:
    path = tmp_path / "cal.json"
    seed_file(path)
    report = await run_update(path, FakeFetcher(), NOW)
    assert report.result == "updated"
    new = read_calendar(path)
    assert new is not None
    assert new.valid_until == datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC)
    assert new.loaded_at == NOW
    assert any(w.label == "FOMC manual" for w in new.windows)
    assert len(new.windows) > 10
    assert read_calendar(sibling(path, ".bak")) is not None
    status = read_status(path)
    assert status is not None
    assert status["result"] == "updated"
    assert status["added"]
    audit = sibling(path, ".audit.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(audit[-1])["result"] == "updated"
    again = await run_update(path, FakeFetcher(), NOW)
    assert again.result == "unchanged"


async def test_manual_file_keeps_windows_and_larger_valid_until(tmp_path: Path) -> None:
    path = tmp_path / "cal.json"
    path.write_text(MANUAL.read_text(encoding="utf-8"), encoding="utf-8")
    manual = EventCalendar.load(MANUAL)
    report = await run_update(path, FakeFetcher(), NOW)
    assert report.result in {"updated", "unchanged"}
    new = read_calendar(path)
    assert new is not None
    assert new.valid_until == manual.valid_until
    assert regression_problem(manual, new) is None or new.loaded_at != manual.loaded_at


@pytest.mark.parametrize("broken", ["fomc", "cpi", "empsit"])
async def test_source_failure_keeps_file_untouched(tmp_path: Path, broken: str) -> None:
    path = tmp_path / "cal.json"
    seed_file(path)
    before = path.read_bytes()
    report = await run_update(path, FakeFetcher(**{broken: "<html>mudou</html>"}), NOW)
    assert report.result == "failed"
    assert report.sources
    assert path.read_bytes() == before
    assert not sibling(path, ".bak").exists()
    status = read_status(path)
    assert status is not None
    assert status["result"] == "failed"


async def test_network_error_is_a_failure_not_an_extension(tmp_path: Path) -> None:
    path = tmp_path / "cal.json"
    seed_file(path)
    before = path.read_bytes()
    report = await run_update(path, FakeFetcher(fomc=CalendarSourceError("timeout")), NOW)
    assert report.result == "failed"
    assert path.read_bytes() == before


async def test_sanity_rejection_keeps_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "cal.json"
    seed_file(path)
    before = path.read_bytes()
    monkeypatch.setattr(calendar_store, "MAX_ADDED_PER_RUN", 1)
    report = await run_update(path, FakeFetcher(), NOW)
    assert report.result == "rejected"
    assert "limite" in report.detail
    assert path.read_bytes() == before


async def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    path = tmp_path / "cal.json"
    seed_file(path)
    before = path.read_bytes()
    report = await run_update(path, FakeFetcher(), NOW, dry_run=True)
    assert report.result == "updated"
    assert report.dry_run
    assert path.read_bytes() == before
    assert not (tmp_path / "cal.json.bak").exists()
    assert not (tmp_path / "cal.json.status.json").exists()


async def test_invalid_current_file_is_never_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "cal.json"
    path.write_text("{quebrado", encoding="utf-8")
    report = await run_update(path, FakeFetcher(), NOW)
    assert report.result == "failed"
    assert path.read_text(encoding="utf-8") == "{quebrado"


async def test_missing_file_bootstraps_from_seed(tmp_path: Path) -> None:
    seed = tmp_path / "seed.json"
    seed_file(seed)
    path = tmp_path / "mut" / "cal.json"
    report = await run_update(path, FakeFetcher(), NOW, seed=seed)
    assert report.result == "updated"
    new = read_calendar(path)
    assert new is not None
    assert any(w.label == "FOMC manual" for w in new.windows)


def test_reloader_keeps_previous_on_invalid_regressed_or_missing(tmp_path: Path) -> None:
    path = tmp_path / "cal.json"
    current = calendar(
        "2027-01-15T23:59:59+00:00", (window("2026-11-06T13:30:00+00:00", "2026-11-06T14:30:00+00:00"),)
    )
    reloader = CalendarReloader(path)
    assert reloader.reload(current, NOW) is current
    path.write_text("{quebrado", encoding="utf-8")
    assert reloader.reload(current, NOW) is current
    path.write_text(
        calendar_store.serialize(calendar("2026-12-01T00:00:00+00:00", current.windows)), encoding="utf-8"
    )
    assert reloader.reload(current, NOW) is current
    path.write_text(
        calendar_store.serialize(calendar("2027-03-01T00:00:00+00:00", current.windows)), encoding="utf-8"
    )
    fresh = reloader.reload(current, NOW)
    assert fresh is not current
    assert fresh.valid_until == datetime(2027, 3, 1, tzinfo=UTC)
    path.write_text(calendar_store.serialize(EventCalendar()), encoding="utf-8")
    assert reloader.reload(fresh, NOW) is fresh


def test_reloader_prefers_larger_coverage_between_file_and_seed(tmp_path: Path) -> None:
    seed, path = tmp_path / "seed.json", tmp_path / "cal.json"
    seed_file(seed, "2026-12-31T00:00:00+00:00")
    seed_file(path, "2027-03-01T00:00:00+00:00")
    reloaded = CalendarReloader(path, seed).reload(EventCalendar(), NOW)
    assert reloaded.valid_until == datetime(2027, 3, 1, tzinfo=UTC)


async def test_update_advances_when_seed_lacks_already_past_windows(tmp_path: Path) -> None:
    seed, mutable = tmp_path / "seed.json", tmp_path / "mut.json"
    fomc = window("2026-10-28T18:00:00+00:00", "2026-10-28T19:30:00+00:00", "FOMC")
    seed.write_text(
        calendar_store.serialize(calendar("2027-01-15T23:59:59+00:00", (fomc,))), encoding="utf-8"
    )
    old = window("2026-09-01T12:00:00+00:00", "2026-09-01T13:00:00+00:00", "velha")
    mutable.write_text(
        calendar_store.serialize(calendar("2026-11-30T23:59:59+00:00", (old, fomc))), encoding="utf-8"
    )
    report = await run_update(mutable, FakeFetcher(), NOW, seed=seed)
    assert report.result == "updated", report.detail


def test_regression_still_rejects_removed_or_shortened_future_window() -> None:
    future = window("2026-11-10T18:00:00+00:00", "2026-11-10T19:30:00+00:00", "FOMC")
    base = calendar("2026-11-30T23:59:59+00:00", (future,))
    shorter = window("2026-11-10T18:00:00+00:00", "2026-11-10T19:00:00+00:00", "FOMC")
    for windows in ((), (shorter,)):
        problem = regression_problem(base, calendar("2027-01-15T23:59:59+00:00", windows), NOW)
        assert problem is not None and "janela existente" in problem
    ongoing = window("2026-10-25T19:00:00+00:00", "2026-10-25T21:00:00+00:00", "em curso")
    assert regression_problem(
        calendar("2026-11-30T23:59:59+00:00", (ongoing,)), calendar("2027-01-15T23:59:59+00:00"), NOW
    )
