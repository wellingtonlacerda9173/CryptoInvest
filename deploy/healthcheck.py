"""Healthcheck dos containers Python (sem dependencias): `healthcheck.py api|runner|calendar`.

api: GET /health local. runner: o `status.json` do ultimo run precisa ter sido regravado ha menos de
`HEALTHCHECK_RUNNER_MAX_AGE_SECONDS` (o robo o regrava a cada heartbeat, 30 s por padrao).
"""

import json
import os
import sys
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

DATA_DIR = Path(os.environ.get("HEALTHCHECK_RUNNER_DATA_DIR", "/var/lib/cryptoinvest/paper"))
MAX_AGE = float(os.environ.get("HEALTHCHECK_RUNNER_MAX_AGE_SECONDS", "150"))


def check_api() -> bool:
    port = os.environ.get("HEALTHCHECK_API_PORT", "8000")
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=4) as response:
        return bool(response.status == 200)


def check_runner() -> bool:
    latest = DATA_DIR / "latest"
    if not latest.is_file():
        return False
    run_id = latest.read_text(encoding="utf-8").strip()
    status = DATA_DIR / run_id / "status.json"
    return status.is_file() and time.time() - status.stat().st_mtime < MAX_AGE


CALENDAR = Path(
    os.environ.get("HEALTHCHECK_CALENDAR_FILE", "/var/lib/cryptoinvest/calendar/volatility_events.json")
)
CALENDAR_MAX_ATTEMPT_AGE = float(os.environ.get("HEALTHCHECK_CALENDAR_MAX_ATTEMPT_AGE_SECONDS", "93600"))
CALENDAR_MIN_COVERAGE_SECONDS = 7 * 86400


def _moment(value: object) -> float | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC).timestamp()
    except ValueError:
        return None


def calendar_health(status: dict[str, object], valid_until: object, now: float) -> bool:
    """Saudavel: ultima tentativa recente E bem-sucedida, OU cobertura restante > 7 dias."""
    attempted = _moment(status.get("attempted_at"))
    recent_ok = (
        status.get("result") in ("updated", "unchanged")
        and attempted is not None
        and 0 <= now - attempted < CALENDAR_MAX_ATTEMPT_AGE
    )
    until = _moment(valid_until)
    return recent_ok or (until is not None and until - now > CALENDAR_MIN_COVERAGE_SECONDS)


def check_calendar() -> bool:
    status = json.loads(CALENDAR.with_name(CALENDAR.name + ".status.json").read_text(encoding="utf-8"))
    try:
        valid_until = json.loads(CALENDAR.read_text(encoding="utf-8")).get("valid_until")
    except (OSError, ValueError):
        valid_until = None
    return calendar_health(status, valid_until, time.time())


def main(argv: list[str]) -> int:
    checks = {"api": check_api, "runner": check_runner, "calendar": check_calendar}
    if len(argv) != 2 or argv[1] not in checks:
        print("uso: healthcheck.py api|runner|calendar", file=sys.stderr)
        return 2
    try:
        return 0 if checks[argv[1]]() else 1
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
