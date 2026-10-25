from __future__ import annotations

import argparse
from pathlib import Path

CACHE_DIR = Path("data/cache/lp")
DATASET = CACHE_DIR / "dataset.json"
REPORT_DIR = Path("reports/lp")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m engine.research.lp", description="Pesquisa LP (somente analise)"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    fetch = sub.add_parser("fetch", help="baixa e cacheia dados publicos (HTTP GET) e monta o dataset")
    fetch.add_argument("--ttl-hours", type=float, default=24.0)
    sim = sub.add_parser("simulate", help="roda as estrategias sobre o dataset")
    sim.add_argument("--mode", choices=["primary", "sensitivity", "stress"], default="primary")
    sub.add_parser("report", help="gera Markdown e JSON em reports/lp/")
    nf = sub.add_parser("narrow-fetch", help="faixas estreitas: OHLCV horario (GeckoTerminal)")
    nf.add_argument("--ttl-hours", type=float, default=24.0)
    sub.add_parser("narrow-snapshot", help="faixas estreitas: snapshot de ticks via RPC publico (leitura)")
    sub.add_parser("narrow-calibrate", help="faixas estreitas: replay de eventos Swap vs modelo")
    nr = sub.add_parser("narrow-run", help="faixas estreitas: simulacao")
    nr.add_argument("--mode", choices=["primary", "sensitivity"], default="primary")
    sub.add_parser("narrow-report", help="faixas estreitas: relatorio em Markdown para leigo")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd.startswith("narrow-"):
        from engine.research.lp.narrow import commands

        match args.cmd:
            case "narrow-fetch":
                print(commands.cmd_fetch(args.ttl_hours))
            case "narrow-snapshot":
                print(commands.cmd_snapshot())
            case "narrow-calibrate":
                print(commands.cmd_calibrate())
            case "narrow-run":
                print(commands.cmd_run(args.mode))
            case _:
                from engine.research.lp.narrow.report import write_narrow_report

                print(
                    write_narrow_report(
                        commands.PRIMARY, commands.SENSITIVITY, commands.CALIBRATION, commands.REPORT
                    )
                )
        return 0
    if args.cmd == "fetch":
        from engine.research.lp.fetcher import FetchParams, fetch_dataset

        ds = fetch_dataset(FetchParams(CACHE_DIR, DATASET, args.ttl_hours))
        print(f"{len(ds['pools'])} pools, {len(ds['skipped'])} descartados -> {DATASET}")
    elif args.cmd == "simulate":
        from engine.research.lp.runner import run_mode

        print(run_mode(DATASET, REPORT_DIR, args.mode))
    else:
        from engine.research.lp.report import write_report

        print(write_report(REPORT_DIR))
    return 0
