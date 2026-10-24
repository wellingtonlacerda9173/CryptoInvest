"""CLI da validacao fora da amostra: `python -m engine.runner.validate --symbols BTC/USDT,ETH/USDT
--timeframes 1h,4h --years 4 --end 2026-10-01T00:00:00Z`.

Roda TODAS as estrategias do registry com o BacktestEngine, classifica os regimes, treina/avalia o seletor em
walk-forward e compara tudo com as referencias nulas (comprar e segurar e entradas aleatorias), com
correcao de multiplas comparacoes sobre a familia inteira (pares x timeframes x estrategias + seletor).
Deterministico: `--end` fixo, sementes fixas, relogio do relatorio = `--end`.
"""

import argparse
import asyncio
import json
import sys
from collections.abc import Awaitable, Callable, Sequence
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta
from decimal import Decimal
from functools import partial
from pathlib import Path
from typing import Any

from engine.application.backtest import BacktestSettings, ReportMeta, default_rules
from engine.application.backtest.report import _write_json
from engine.application.regime import RegimeConfig
from engine.application.risk import SymbolRules
from engine.application.selector import TradeSample, build_scores
from engine.application.strategies import Strategy
from engine.application.strategies.registry import STRATEGY_REGISTRY, is_applicable
from engine.application.validation import (
    ValidationConfig,
    ValidationReport,
    Verdict,
    collect_runs,
    corrected_alpha,
    min_attainable_p,
    required_simulations,
    validate_samples,
    write_validation_report,
)
from engine.application.validation.idle import Span
from engine.application.validation.summary import (
    CellInfo,
    ValidationSummary,
    build_summary,
    render_summary_markdown,
)
from engine.domain.candle import Candle
from engine.domain.risk import RiskConfig
from engine.domain.types import Timeframe
from engine.infrastructure.config.settings import load_settings
from engine.infrastructure.persistence.session import create_engine, create_session_factory
from engine.runner.backtest import git_version, parse_instant, portable
from engine.runner.candle_source import floor_to_timeframe, load_candles
from engine.runner.selector_store import (
    DEFAULT_SAMPLES_DIR,
    SqlScoreStore,
    read_samples,
    samples_path,
    write_samples,
)

_YEAR_DAYS = 365
EXIT_OK, EXIT_USAGE, EXIT_NOT_VALIDATED = 0, 1, 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m engine.runner.validate", description=__doc__)
    parser.add_argument("--symbols", default="BTC/USDT,ETH/USDT,SOL/USDT")
    parser.add_argument("--timeframes", default="1h,4h")
    parser.add_argument("--years", type=float, default=4.0)
    parser.add_argument("--end", required=True, help="fim dos dados (ISO 8601; sem fuso = UTC); obrigatorio")
    parser.add_argument(
        "--strategies", default="all", help="'all' ou nomes do registry separados por virgula"
    )
    parser.add_argument(
        "--simulations",
        type=int,
        default=None,
        help="sorteios aleatorios; padrao = max(10000, ceil(1,2 / alfa corrigido))",
    )
    parser.add_argument(
        "--allow-underpowered",
        action="store_true",
        help="aceita --simulations com menor p atingivel >= alfa corrigido (nenhum 'supera' e possivel)",
    )
    parser.add_argument("--seed", type=int, default=ValidationConfig().seed)
    parser.add_argument("--alpha", type=float, default=0.05)
    parser.add_argument("--correction", choices=["sidak", "bonferroni"], default="sidak")
    parser.add_argument("--train-days", type=int, default=180)
    parser.add_argument("--test-days", type=int, default=60)
    parser.add_argument("--capital", type=Decimal, default=Decimal(10_000))
    parser.add_argument("--taker-fee", type=Decimal, default=Decimal("0.001"))
    parser.add_argument("--maker-fee", type=Decimal, default=Decimal("0.001"))
    parser.add_argument("--slippage-bps", type=Decimal, default=Decimal(5))
    parser.add_argument("--latency", type=int, default=1)
    parser.add_argument("--out", type=Path, default=Path("reports/validation"))
    parser.add_argument("--cache-dir", type=Path, default=Path("data/cache"))
    parser.add_argument("--no-db", action="store_true", help="nao tenta o banco Timescale")
    parser.add_argument("--refresh", action="store_true", help="ignora o cache local de velas")
    parser.add_argument("--workers", type=int, default=1, help="processos para rodar as celulas em paralelo")
    parser.add_argument("--resume", action="store_true", help="reaproveita samples.jsonl ja calculados")
    parser.add_argument(
        "--publish-scores",
        action="store_true",
        help="grava no banco os scores do backtest validado (so celulas em que o seletor passou)",
    )
    parser.add_argument(
        "--allow-unvalidated",
        action="store_true",
        help="com --publish-scores, publica mesmo sem veredito favoravel (NAO recomendado)",
    )
    parser.add_argument("--samples-dir", type=Path, default=DEFAULT_SAMPLES_DIR)
    return parser


def select_strategies(spec: str) -> list[Strategy]:
    names = (
        list(STRATEGY_REGISTRY)
        if spec.strip() == "all"
        else [n.strip() for n in spec.split(",") if n.strip()]
    )
    if unknown := [n for n in names if n not in STRATEGY_REGISTRY]:
        raise ValueError(f"estrategias desconhecidas {unknown}; validas: {list(STRATEGY_REGISTRY)}")
    return [STRATEGY_REGISTRY[n]() for n in names]


def _settings(args: argparse.Namespace) -> BacktestSettings:
    return BacktestSettings(
        initial_capital=args.capital,
        maker_fee_rate=args.maker_fee,
        slippage_bps=args.slippage_bps,
        latency_candles=args.latency,
        risk=RiskConfig(taker_fee_rate=args.taker_fee),
    )


Collected = tuple[list[TradeSample], dict[str, list[Span]]]


def _collect_worker(
    candles: list[Candle],
    names: list[str],
    settings: BacktestSettings,
    regime: RegimeConfig,
    rules: SymbolRules,
    end: datetime,
) -> Collected:
    strategies = [STRATEGY_REGISTRY[n]() for n in names]
    return asyncio.run(collect_runs(candles, strategies, settings, regime, rules=rules, clock=lambda: end))


async def _ready(value: Collected) -> Collected:
    return value


async def _ready_future(future: Awaitable[Collected]) -> Collected:
    return await future


def _cell_dir(out: Path, symbol: str, timeframe: str) -> Path:
    return out / f"{symbol.replace('/', '')}_{timeframe}"


def _save_cell(directory: Path, samples: Sequence[TradeSample], idle: dict[str, list[Span]]) -> None:
    write_samples(directory / "samples.jsonl", samples)
    payload = {k: [[a.isoformat(), b.isoformat()] for a, b in v] for k, v in idle.items()}
    (directory / "idle.json").write_text(json.dumps(payload), encoding="utf-8")


def _load_cell(directory: Path) -> Collected | None:
    """So `idle.json` marca coleta sem halt de drawdown: caches antigos (`halts.json`) sao descartados."""
    samples_file, idle_file = directory / "samples.jsonl", directory / "idle.json"
    if not (samples_file.is_file() and idle_file.is_file()):
        return None
    raw = json.loads(idle_file.read_text(encoding="utf-8"))
    idle = {k: [(datetime.fromisoformat(a), datetime.fromisoformat(b)) for a, b in v] for k, v in raw.items()}
    return read_samples(samples_file), idle


def _log(message: str) -> None:
    print(message, flush=True)


async def run_cells(args: argparse.Namespace, end: datetime, out: Path) -> list[tuple[CellInfo, list[Any]]]:
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    timeframes = [Timeframe(t.strip()) for t in args.timeframes.split(",") if t.strip()]
    names = [s.name for s in select_strategies(args.strategies)]
    settings = _settings(args)
    regime = RegimeConfig()
    loop = asyncio.get_running_loop()
    pool = ProcessPoolExecutor(max_workers=args.workers) if args.workers > 1 else None
    pending: list[tuple[str, Timeframe, list[Candle], Callable[[], Awaitable[Collected]], CellInfo]] = []
    for timeframe in timeframes:
        cell_end = floor_to_timeframe(end, timeframe)
        start = cell_end - timedelta(days=round(args.years * _YEAR_DAYS))
        for symbol in symbols:
            loaded = await load_candles(
                symbol,
                timeframe,
                start,
                cell_end,
                cache_dir=args.cache_dir,
                use_db=not args.no_db,
                refresh=args.refresh,
            )
            candles = loaded.candles
            if len(candles) < 2:
                raise SystemExit(f"{symbol} {timeframe.value}: sem velas ({loaded.source})")
            _log(f"[dados] {symbol} {timeframe.value}: {len(candles)} velas ({loaded.source})")
            info = CellInfo(
                symbol=symbol,
                timeframe=timeframe.value,
                candles=len(candles),
                period_start=candles[0].open_time,
                period_end=candles[-1].close_time,
                source=loaded.source,
                notes=loaded.notes,
            )
            directory = _cell_dir(out, symbol, timeframe.value)
            cached = _load_cell(directory) if args.resume else None
            job: Callable[[], Awaitable[Collected]]
            if cached is not None:
                _log(f"[resume] {symbol} {timeframe.value}: amostras reaproveitadas")
                job = partial(_ready, cached)
            else:
                rules = default_rules(symbol)
                job_args = (candles, names, settings, regime, rules, end)
                if pool is None:
                    job = partial(asyncio.to_thread, _collect_worker, *job_args)
                else:
                    job = partial(_ready_future, loop.run_in_executor(pool, _collect_worker, *job_args))
            pending.append((symbol, timeframe, candles, job, info))
    results: list[tuple[CellInfo, list[Any]]] = []
    try:
        for symbol, timeframe, candles, job, info in pending:
            _log(f"[backtest] {symbol} {timeframe.value}: {len(names)} estrategias...")
            samples, idle = await job()
            _save_cell(_cell_dir(out, symbol, timeframe.value), samples, idle)
            _log(f"[ok] {symbol} {timeframe.value}: {len(samples)} trades de {len(names)} estrategias")
            results.append((info, [candles, samples, idle]))
    finally:
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)
    return results


def count_comparisons(names: Sequence[str], symbols: Sequence[str], timeframes: Sequence[str]) -> int:
    """Estrategias aplicaveis em cada timeframe + o seletor, por par (ex.: vwap nao opera em 4h)."""
    per_symbol = sum(sum(1 for n in names if is_applicable(n, Timeframe(tf))) + 1 for tf in timeframes)
    return per_symbol * len(symbols)


def resolve_simulations(args: argparse.Namespace, comparisons: int) -> int:
    alpha_c = corrected_alpha(args.alpha, comparisons, args.correction)
    if args.simulations is None:
        return required_simulations(alpha_c)
    if min_attainable_p(args.simulations) >= alpha_c and not args.allow_underpowered:
        raise ValueError(
            f"teste sem poder: com {args.simulations} simulações o menor p atingível "
            f"({min_attainable_p(args.simulations):.6f}) é >= alfa corrigido {alpha_c:.6f} "
            f"(m={comparisons}); use ao menos {required_simulations(alpha_c)} ou --allow-underpowered"
        )
    return int(args.simulations)


def _config(
    args: argparse.Namespace, comparisons_total: int, strategies: int, simulations: int
) -> ValidationConfig:
    cost = args.maker_fee + args.taker_fee + 2 * args.slippage_bps / 10_000
    return ValidationConfig(
        train_days_min=args.train_days,
        test_days=args.test_days,
        simulations=simulations,
        seed=args.seed,
        alpha=args.alpha,
        correction=args.correction,
        extra_comparisons=comparisons_total - (strategies + 1),
        cost_pct=float(cost),
    )


def _meta(
    args: argparse.Namespace, end: datetime, cells: Sequence[CellInfo], names: Sequence[str]
) -> ReportMeta:
    sha, dirty = git_version()
    return ReportMeta(
        name=f"validacao_{end:%Y%m%d}_{args.years:g}a",
        git_sha=sha,
        git_dirty=dirty,
        data_source="; ".join(sorted({c.source for c in cells})),
        data_notes=tuple(f"{c.symbol} {c.timeframe}: {c.candles} velas" for c in cells),
        parameters={
            "cli": {k: portable(v) if isinstance(v, Path) else str(v) for k, v in vars(args).items()},
            "end": end.isoformat(),
            "strategies": list(names),
        },
    )


async def publish_scores(
    args: argparse.Namespace,
    summary: ValidationSummary,
    cell_samples: dict[tuple[str, str], list[TradeSample]],
    end: datetime,
) -> int:
    approved = {
        (r.symbol, r.timeframe)
        for r in summary.reports
        if r.selector.judgement.verdict is Verdict.SUPERA or args.allow_unvalidated
    }
    if not approved:
        _log("[publicacao] recusada: o seletor nao superou as referencias em nenhuma celula")
        return EXIT_NOT_VALIDATED
    selector = ValidationConfig().selector
    db = create_engine(load_settings().database_url)
    store = SqlScoreStore(create_session_factory(db))
    try:
        for timeframe in sorted({tf for _, tf in approved}):
            pool = [
                s
                for (sym, tf), items in cell_samples.items()
                if (sym, tf) in approved and tf == timeframe
                for s in items
            ]
            scores = build_scores(pool, end, selector)
            snapshot = await store.publish(
                list(scores.values()), timeframe=timeframe, origin="backtest", calculated_at=end
            )
            write_samples(samples_path(args.samples_dir, timeframe), pool)
            _log(f"[publicacao] {timeframe}: {len(scores)} scores no snapshot {snapshot}")
    finally:
        await db.dispose()
    return EXIT_OK


async def main_async(args: argparse.Namespace) -> int:
    end = parse_instant(args.end)
    names = [s.name for s in select_strategies(args.strategies)]
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    timeframes = [t.strip() for t in args.timeframes.split(",") if t.strip()]
    total = count_comparisons(names, symbols, timeframes)
    simulations = resolve_simulations(args, total)
    args.simulations = simulations
    out = args.out / f"{end:%Y%m%dT%H%M}_{args.years:g}a"
    out.mkdir(parents=True, exist_ok=True)
    cells = await run_cells(args, end, out)
    config = _config(args, total, len(names), simulations)
    reports: list[ValidationReport] = []
    meta = _meta(args, end, [c for c, _ in cells], names)
    cell_samples: dict[tuple[str, str], list[TradeSample]] = {}
    for info, (candles, samples, idle) in cells:
        report = validate_samples(candles, samples, names, config, generated_at=end, idle=idle)
        reports.append(report)
        cell_samples[(info.symbol, info.timeframe)] = samples
        write_validation_report(_cell_dir(out, info.symbol, info.timeframe), report, meta)
        _log(f"[validacao] {info.symbol} {info.timeframe}: seletor {report.selector.judgement.verdict.value}")
    summary = build_summary(reports, [c for c, _ in cells], end)
    _write_json(
        out / "summary.json",
        {"meta": meta.model_dump(mode="json"), "summary": summary.model_dump(mode="json")},
    )
    (out / "summary.md").write_text(render_summary_markdown(summary, meta), encoding="utf-8")
    _log(f"[relatorio] {portable(out)}")
    _log(summary.conclusion)
    if args.publish_scores:
        return await publish_scores(args, summary, cell_samples, end)
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        code = asyncio.run(main_async(args))
    except (ValueError, OSError) as error:
        print(f"erro: {error}", file=sys.stderr)
        code = EXIT_USAGE
    raise SystemExit(code)


if __name__ == "__main__":
    main()
