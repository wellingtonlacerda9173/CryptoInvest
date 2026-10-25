"""Healthcheck dos containers Python (sem dependencias): `healthcheck.py api|runner`.

api: GET /health local. runner: o `status.json` do ultimo run precisa ter sido regravado ha menos de
`HEALTHCHECK_RUNNER_MAX_AGE_SECONDS` (o robo o regrava a cada heartbeat, 30 s por padrao).
"""

import os
import sys
import time
import urllib.request
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


def main(argv: list[str]) -> int:
    checks = {"api": check_api, "runner": check_runner}
    if len(argv) != 2 or argv[1] not in checks:
        print("uso: healthcheck.py api|runner", file=sys.stderr)
        return 2
    try:
        return 0 if checks[argv[1]]() else 1
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
