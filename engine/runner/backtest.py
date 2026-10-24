"""CLI do backtest: `uv run python -m engine.runner.backtest --symbol BTC/USDT --years 2 ...`."""

import argparse
import asyncio
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from engine.application.backtest import (
    BacktestEngine,
    BacktestSettings,
    NotEnoughCandlesError,
    ParameterGrid,
    ReportMeta,
    WalkForwardConfig,
    compute_metrics,
    default_rules,
    summarize_avoided,
    walk_forward,
    write_backtest_report,
    write_walk_forward_report,
)
from engine.application.risk import SymbolRules
from engine.application.strategies import (
    ElephantBarBreakout,
    ElephantBarBreakoutConfig,
    EmaCrossTrend,
    EmaCrossTrendConfig,
    LevelsConfig,
    StopMode,
    Strategy,
)
from engine.application.strategies.registry import STRATEGY_REGISTRY
from engine.domain.risk import RiskConfig
from engine.domain.types import Timeframe
from engine.runner.candle_source import floor_to_timeframe, load_candles

STRATEGY_NAMES = ("elephant", "ema")
PARAMETERS = frozenset({"min_confidence", "rsi_min", "reward_risk"})
DEFAULT_GRID = "min_confidence=0.5,0.6;rsi_min=45,50;reward_risk=2,3"
_EMA_RSI_MAX = 70.0
_YEAR_DAYS = 365


def parse_values(text: str) -> dict[str, Decimal]:
    """`a=1,b=2` -> {a: 1, b: 2} (parametros fixos das estrategias)."""
    pairs = (item.split("=", 1) for item in text.split(",") if item.strip())
    params = {key.strip(): Decimal(value.strip()) for key, value in pairs}
    _check_names(params)
    return params


def parse_grid(text: str) -> ParameterGrid:
    """`a=1,2;b=3,4` -> grade com todas as combinacoes."""
    values: dict[str, tuple[Decimal, ...]] = {}
    for item in filter(None, (part.strip() for part in text.split(";"))):
        name, _, raw = item.partition("=")
        values[name.strip()] = tuple(Decimal(v.strip()) for v in raw.split(",") if v.strip())
    _check_names(values)
    if not values or any(not v for v in values.values()):
        raise ValueError("a grade precisa de ao menos um valor por parametro")
    return ParameterGrid(values=values)


def _check_names(params: Mapping[str, object]) -> None:
    if unknown := sorted(set(params) - PARAMETERS):
        raise ValueError(f"parametros desconhecidos {unknown}; validos: {sorted(PARAMETERS)}")


def _levels(params: Mapping[str, Decimal], stop_mode: StopMode | None = None) -> LevelsConfig:
    kwargs: dict[str, object] = {}
    if "reward_risk" in params:
        kwargs["reward_risk"] = params["reward_risk"]
    if stop_mode is not None:
        kwargs["stop_mode"] = stop_mode
    return LevelsConfig.model_validate(kwargs)


def build_strategies(names: Sequence[str], params: Mapping[str, Decimal]) -> list[Strategy]:
    """Parametros ausentes mantem o padrao da estrategia."""
    confidence = {"min_confidence": float(params["min_confidence"])} if "min_confidence" in params else {}
    rsi_min = float(params["rsi_min"]) if "rsi_min" in params else None
    strategies: list[Strategy] = []
    for name in names:
        if name == "elephant":
            extra = {} if rsi_min is None else {"rsi_min": rsi_min}
            config = ElephantBarBreakoutConfig(levels=_levels(params), **confidence, **extra)
            strategies.append(ElephantBarBreakout(config))
        elif name == "ema":
            rsi = {} if rsi_min is None else {"rsi_range": (rsi_min, _EMA_RSI_MAX)}
            config_ema = EmaCrossTrendConfig(levels=_levels(params, StopMode.ATR), **confidence, **rsi)
            strategies.append(EmaCrossTrend(config_ema))
        elif name == "all":
            strategies += [factory() for factory in STRATEGY_REGISTRY.values()]
        elif name in STRATEGY_REGISTRY:
            strategies.append(STRATEGY_REGISTRY[name]())
        else:
            raise ValueError(f"estrategia desconhecida '{name}'; validas: {list(STRATEGY_NAMES)}")
    return strategies


def git_version() -> tuple[str | None, bool | None]:
    git = shutil.which("git")
    if git is None:
        return None, None
    try:
        sha = subprocess.run(  # noqa: S603 - executavel resolvido por shutil.which, argumentos fixos
            [git, "rev-parse", "HEAD"], capture_output=True, text=True, check=True, timeout=10
        ).stdout.strip()
        dirty = subprocess.run(  # noqa: S603
            [git, "status", "--porcelain"], capture_output=True, text=True, check=True, timeout=10
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return None, None
    return sha, bool(dirty)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m engine.runner.backtest", description=__doc__)
    parser.add_argument("--symbol", default="BTC/USDT", help="um ou mais pares separados por virgula")
    parser.add_argument("--timeframe", default="1h", choices=[t.value for t in Timeframe])
    parser.add_argument("--years", type=float, default=2.0)
    parser.add_argument("--end", help="fim do periodo (ISO 8601; sem fuso = UTC); padrao: agora")
    parser.add_argument(
        "--strategies", default="elephant,ema", help=f"{STRATEGY_NAMES}, separadas por virgula"
    )
    parser.add_argument("--params", default="", help="parametros fixos, ex.: min_confidence=0.5,rsi_min=50")
    parser.add_argument("--capital", type=Decimal, default=Decimal(10_000))
    parser.add_argument("--taker-fee", type=Decimal, default=Decimal("0.001"))
    parser.add_argument("--maker-fee", type=Decimal, default=Decimal("0.001"))
    parser.add_argument("--slippage-bps", type=Decimal, default=Decimal(5))
    parser.add_argument("--latency", type=int, default=1, help="velas entre a decisao e a execucao")
    parser.add_argument("--tick-size", type=Decimal)
    parser.add_argument("--step-size", type=Decimal)
    parser.add_argument("--min-notional", type=Decimal)
    parser.add_argument("--walk-forward", action="store_true")
    parser.add_argument("--train-days", type=int, default=180)
    parser.add_argument("--test-days", type=int, default=60)
    parser.add_argument("--step-days", type=int)
    parser.add_argument("--min-train-trades", type=int, default=5)
    parser.add_argument("--grid", default=DEFAULT_GRID)
    parser.add_argument("--out", type=Path, default=Path("reports"))
    parser.add_argument("--cache-dir", type=Path, default=Path("data/cache"))
    parser.add_argument("--no-db", action="store_true", help="nao tenta o banco Timescale")
    parser.add_argument("--refresh", action="store_true", help="ignora o cache local")
    return parser


def _rules(symbol: str, args: argparse.Namespace) -> SymbolRules:
    base = default_rules(symbol)
    update: dict[str, Decimal] = {}
    if args.tick_size:
        update["tick_size"] = args.tick_size
    if args.step_size:
        update.update(step_size=args.step_size, min_quantity=args.step_size)
    if args.min_notional:
        update["min_notional"] = args.min_notional
    return base.model_copy(update=update)


def parse_instant(text: str) -> datetime:
    """ISO 8601; sem fuso => UTC (nunca o fuso local da maquina)."""
    moment = datetime.fromisoformat(text)
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def resolve_period(
    args: argparse.Namespace, now: datetime, timeframe: Timeframe
) -> tuple[datetime, datetime]:
    end = floor_to_timeframe(parse_instant(args.end) if args.end else now, timeframe)
    return end - timedelta(days=round(args.years * _YEAR_DAYS)), end


def portable(path: Path) -> str:
    """Caminho relativo ao diretorio de trabalho (ou so o nome) para nao vazar o ambiente nos relatorios."""
    try:
        return (
            path.resolve().relative_to(Path.cwd().resolve()).as_posix()
            if path.is_absolute()
            else path.as_posix()
        )
    except ValueError:
        return path.name


def _pct(value: float | None) -> str:
    return "n/d" if value is None else f"{value * 100:.2f}%"


async def run_symbol(args: argparse.Namespace, symbol: str, now: datetime) -> bool:
    timeframe = Timeframe(args.timeframe)
    names = [n.strip() for n in args.strategies.split(",") if n.strip()]
    start, end = resolve_period(args, now, timeframe)
    loaded = await load_candles(
        symbol, timeframe, start, end, cache_dir=args.cache_dir, use_db=not args.no_db, refresh=args.refresh
    )
    candles = loaded.candles
    if len(candles) < 2:
        raise SystemExit(f"{symbol}: sem velas suficientes ({loaded.source})")
    settings = BacktestSettings(
        initial_capital=args.capital,
        maker_fee_rate=args.maker_fee,
        slippage_bps=args.slippage_bps,
        latency_candles=args.latency,
        risk=RiskConfig(taker_fee_rate=args.taker_fee),
    )
    rules = _rules(symbol, args)
    sha, dirty = git_version()
    fixed = parse_values(args.params)
    label = f"{symbol.replace('/', '')}_{timeframe.value}_{'-'.join(names)}"
    meta = ReportMeta(
        name=label,
        git_sha=sha,
        git_dirty=dirty,
        data_source=loaded.source,
        data_notes=(
            f"{len(candles)} velas {candles[0].open_time:%Y-%m-%d %H:%M} a "
            f"{candles[-1].close_time:%Y-%m-%d %H:%M} UTC",
            *loaded.notes,
        ),
        parameters={
            "cli": {k: portable(v) if isinstance(v, Path) else str(v) for k, v in vars(args).items()},
            "period": {"start": start.isoformat(), "end": end.isoformat()},
            "strategy_params": {k: str(v) for k, v in fixed.items()} or "padrao das estrategias",
            "rules": rules.model_dump(mode="json"),
        },
    )
    out_dir = args.out / label
    result = await BacktestEngine(
        build_strategies(names, fixed), settings, rules=rules, clock=lambda: now
    ).run(candles)
    metrics = compute_metrics(result)
    write_backtest_report(out_dir, result, metrics, meta)
    avoided = summarize_avoided(result.decisions)
    filtered = sum(g.signals for g in avoided.by_code if g.kind == "filter")
    filter_effect = (
        f"{avoided.risk_vetoes_net_effect:.2f}" if filtered else "n/a (0 sinais barrados por filtro)"
    )
    print(f"\n=== {symbol} {timeframe.value} | {', '.join(names)} | {len(candles)} velas | {loaded.source}")
    print(
        f"drawdown maximo {_pct(metrics.max_drawdown)} | "
        f"meses positivos {_pct(metrics.positive_months_ratio)} | "
        f"retorno {_pct(metrics.total_return)} | trades {metrics.trades} | acerto {_pct(metrics.win_rate)} | "
        f"parado {_pct(metrics.idle_pct)} | operacoes evitadas {avoided.signals} "
        f"(efeito liquido dos filtros {filter_effect})"
    )
    if args.walk_forward:
        config = WalkForwardConfig(
            train_days=args.train_days,
            test_days=args.test_days,
            step_days=args.step_days,
            min_train_trades=args.min_train_trades,
        )
        try:
            wf = await walk_forward(
                candles,
                lambda params: build_strategies(names, params),
                parse_grid(args.grid),
                settings,
                config,
                rules=rules,
                clock=lambda: now,
            )
        except NotEnoughCandlesError as error:
            print(f"walk-forward nao executado: {error}", file=sys.stderr)
            print(f"relatorios em {portable(out_dir)}")
            return False
        write_walk_forward_report(out_dir, wf, symbol, timeframe.value, meta)
        oos = wf.out_of_sample
        print(
            f"walk-forward ({len(wf.windows)} janelas) fora da amostra: drawdown {_pct(oos.max_drawdown)} | "
            f"retorno {_pct(oos.total_return)} | trades {oos.trades} | veredito: {wf.assessment.verdict}"
        )
    print(f"relatorios em {portable(out_dir)}")
    return True


async def main_async(args: argparse.Namespace) -> bool:
    now = datetime.now(UTC)
    symbols = [s.strip() for s in args.symbol.split(",") if s.strip()]
    return all([await run_symbol(args, symbol, now) for symbol in symbols])


def main(argv: Sequence[str] | None = None) -> None:
    if not asyncio.run(main_async(build_parser().parse_args(argv))):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
