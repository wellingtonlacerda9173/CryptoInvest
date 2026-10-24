"""Gera `api/openapi.json` e `api/ws_schema.json` deterministicos (sem banco, sem segredos reais).

python -m api.export_openapi           # grava api/openapi.json e api/ws_schema.json
python -m api.export_openapi --check   # falha (codigo 1) se algum dos dois estiver desatualizado
"""

import argparse
import json
import sys
from pathlib import Path

from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.app import create_app
from api.config import ApiSettings
from api.ws_schema import build_ws_schema_text

OPENAPI_PATH = Path(__file__).with_name("openapi.json")
WS_SCHEMA_PATH = Path(__file__).with_name("ws_schema.json")


def build_schema_text() -> str:
    settings = ApiSettings(
        environment="test",
        session_secret=SecretStr("x" * 48),
        master_keys=SecretStr("1:" + "A" * 43 + "="),
        panel_origins=["http://localhost:3000"],
    )
    sessions: async_sessionmaker[AsyncSession] = async_sessionmaker()
    schema = create_app(settings, sessions=sessions).openapi()
    return json.dumps(schema, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    files = {OPENAPI_PATH: build_schema_text(), WS_SCHEMA_PATH: build_ws_schema_text()}
    if args.check:
        stale = [
            p.name for p, text in files.items() if not p.exists() or p.read_text(encoding="utf-8") != text
        ]
        for name in stale:
            print(f"api/{name} desatualizado: rode `python -m api.export_openapi`.", file=sys.stderr)
        return 1 if stale else 0
    for path, text in files.items():
        path.write_text(text, encoding="utf-8", newline="\n")
        print(f"Escrito {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
