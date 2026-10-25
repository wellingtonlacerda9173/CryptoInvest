"""Sobe a API com o limite de mensagem do WebSocket aplicado no servidor (antes de ler a mensagem inteira).

    python -m api.serve [--host 127.0.0.1] [--port 8000]

O `max_size` do uvicorn recusa o quadro grande demais NA LEITURA (fecha com 1009); o teto no handler
(`MAX_CLIENT_MESSAGE`) continua como segunda barreira. Equivale a
`uvicorn api.app:create_default_app --factory --ws-max-size 8192`. Um unico processo da API (sem
`--workers`): backtests, bomba de comandos e o hub de tempo real assumem um dono.
"""

import argparse
import os

import uvicorn

WS_MAX_SIZE = 8192


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m api.serve")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--skip-schema-check",
        action="store_true",
        help="DEV: nao compara alembic_version com o head do codigo (producao recusa a flag)",
    )
    args = parser.parse_args(argv)
    if args.skip_schema_check:
        os.environ["API_SKIP_SCHEMA_CHECK"] = "true"
    uvicorn.run(
        "api.app:create_default_app",
        factory=True,
        host=args.host,
        port=args.port,
        ws_max_size=WS_MAX_SIZE,
        workers=1,
    )


if __name__ == "__main__":
    main()
