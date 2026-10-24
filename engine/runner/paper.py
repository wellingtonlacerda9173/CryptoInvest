"""CLI do paper trading: `uv run python -m engine.runner.paper <comando> ...` (processo separado da API).

`start` roda o robo (Binance publica, sem chave); os demais comandos falam com o robo em execucao por arquivos
de comando (`pause`, `resume`, `stop`, `emergency-stop`, `reactivate`) ou leem o diario (`status`, `report`).
"""

import argparse
import asyncio
import getpass
import signal
import sys
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import text

from engine.application.backtest import default_rules
from engine.application.risk import EventCalendar, SymbolRules
from engine.application.selector import SelectorSettings
from engine.application.strategies.registry import STRATEGY_REGISTRY
from engine.domain.risk import RiskConfig
from engine.domain.types import Timeframe
from engine.infrastructure.binance.market_data import BinanceMarketData
from engine.infrastructure.config.logging import configure_logging, scrub_text
from engine.infrastructure.config.redis_auth import with_redis_password
from engine.infrastructure.config.settings import Settings, load_settings, resolve_secrets_dir
from engine.infrastructure.notifications import AlertDispatcher, NotifySettings, build_notifier
from engine.infrastructure.paper import SystemClock
from engine.infrastructure.persistence.recovery import load_recovery_state
from engine.infrastructure.persistence.session import create_engine, create_session_factory
from engine.runner.backtest import STRATEGY_NAMES, build_strategies, git_version, parse_values
from engine.runner.command_channel import (
    RedisCommandSource,
    SignedCommandSource,
    SqlAuditSink,
    load_command_key,
)
from engine.runner.paper_config import (
    DEFAULT_CALENDAR,
    DEFAULT_DATA_DIR,
    PaperRunConfig,
    RunPaths,
    read_latest,
    write_latest,
)
from engine.runner.paper_control import (
    COMMAND_TTL,
    LIQUIDATE_TTL,
    Command,
    CommandName,
    CommandQueue,
    read_status,
)
from engine.runner.paper_events import EventBus, NullEventBus, RedisEventBus
from engine.runner.paper_journal import SqlJournal
from engine.runner.paper_lock import AlreadyRunningError, InstanceLock, database_locks
from engine.runner.paper_report import build_cumulative, build_daily, write_report
from engine.runner.paper_report_data import UnknownRunError, collect_inputs
from engine.runner.paper_robot import PaperRobot
from engine.runner.paper_versioned import apply_versioned_config
from engine.runner.selector_store import DEFAULT_SAMPLES_DIR, SqlScoreStore, read_samples, samples_path

log = structlog.get_logger()

EXIT_OK, EXIT_USAGE, EXIT_FATAL = 0, 1, 2
_STATUS_STALE = timedelta(minutes=2)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m engine.runner.paper", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--run-id", help="padrao: o ultimo run iniciado nesta maquina")
        p.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR, help="estado local dos runs")

    start = sub.add_parser("start", help="inicia (ou recupera) um run de paper trading")
    common(start)
    start.add_argument("--symbols", default="BTC/USDT,ETH/USDT")
    start.add_argument("--timeframe", default="1h", choices=[t.value for t in Timeframe])
    start.add_argument("--capital", type=Decimal, default=Decimal(10_000))
    start.add_argument("--strategies", default="elephant,ema", help=f"{STRATEGY_NAMES}")
    start.add_argument("--params", default="", help="parametros fixos, ex.: min_confidence=0.5")
    start.add_argument("--taker-fee", type=Decimal, default=Decimal("0.001"))
    start.add_argument("--maker-fee", type=Decimal, default=Decimal("0.001"))
    start.add_argument("--slippage-bps", type=Decimal, default=Decimal(5))
    start.add_argument("--latency-ms", type=int, default=100)
    start.add_argument("--ws-url", help="WebSocket da Binance (padrao: PAPER_WS_URL, porta 443)")
    start.add_argument("--calendar", type=Path, default=DEFAULT_CALENDAR)
    start.add_argument(
        "--allow-open-calendar",
        action="store_true",
        help="PAPER: opera sem cobertura do calendario de eventos (aviso a cada decisao). Nunca no Live",
    )
    start.add_argument("--reconcile-minutes", type=float, default=5.0)
    start.add_argument("--equity-seconds", type=int, default=60)
    start.add_argument("--heartbeat-seconds", type=int, default=30)
    start.add_argument("--max-stop-blind-seconds", type=int, default=30)
    start.add_argument("--history-window", type=int, default=400)
    start.add_argument(
        "--duration-minutes", type=float, help="encerra sozinho apos este tempo (smoke/testes)"
    )
    start.add_argument("--emergency-liquidate", action="store_true", help="emergency-stop sempre liquida")
    start.add_argument(
        "--selector",
        action="store_true",
        help="liga o seletor por regime (padrao: desligado); sem scores salvos o robo nao opera",
    )
    start.add_argument("--selector-recalc-hours", type=int, default=6)
    start.add_argument(
        "--selector-samples-dir",
        type=Path,
        default=DEFAULT_SAMPLES_DIR,
        help="amostras de backtest importadas no recalculo (geradas por validate --publish-scores)",
    )
    start.add_argument("--no-redis", action="store_true")
    start.add_argument(
        "--no-versioned-config",
        action="store_true",
        help="ignora a configuracao versionada do painel e usa so os argumentos da CLI",
    )
    start.add_argument(
        "--no-signed-commands", action="store_true", help="nao consome o canal Redis de comandos assinados"
    )
    start.add_argument("--log-level", default=None)
    start.add_argument("--json-logs", action="store_true")

    for name, summary in (
        ("pause", "pausa novas entradas (posicoes seguem geridas)"),
        ("resume", "retoma de uma pausa (exige operador e motivo)"),
        ("stop", "parada ordenada: sem novas entradas, estado salvo, stops mantidos"),
        ("emergency-stop", "bloqueia entradas ja; --liquidate vende a mercado como saida MANUAL"),
        ("reactivate", "religa o robo desligado por drawdown (exige operador e motivo)"),
    ):
        p = sub.add_parser(name, help=summary)
        common(p)
        required = name in ("resume", "reactivate")
        extra = "" if required else "; obrigatorio em emergency-stop --liquidate"
        p.add_argument("--operator", required=required, help=f"quem executa a acao (auditado){extra}")
        p.add_argument("--reason", required=required, help=f"motivo da acao (auditado){extra}")
        if name == "emergency-stop":
            p.add_argument("--liquidate", action="store_true")

    status = sub.add_parser("status", help="estado do robo, travas, posicoes e ultimas decisoes")
    common(status)

    report = sub.add_parser("report", help="relatorio diario e acumulado (JSON + Markdown)")
    common(report)
    report.add_argument("--date", help="dia UTC do relatorio diario (AAAA-MM-DD); padrao: hoje")
    report.add_argument("--out", type=Path, default=Path("reports/paper"))
    report.add_argument("--no-compare", action="store_true", help="nao roda o backtest comparativo")
    report.add_argument("--cache-dir", type=Path, default=Path("data/cache"))
    return parser


def _run_id(args: argparse.Namespace, *, create: bool = False) -> str:
    if args.run_id:
        return str(args.run_id)
    if create:
        return f"paper-{datetime.now(UTC):%Y%m%d-%H%M%S}"
    latest = read_latest(args.data_dir)
    if latest is None:
        raise SystemExit("nenhum run encontrado: informe --run-id")
    return latest


def _rules(symbols: Sequence[str]) -> dict[str, SymbolRules]:
    return {s: default_rules(s) for s in symbols}


def build_config(args: argparse.Namespace) -> PaperRunConfig:
    symbols = tuple(s.strip().upper() for s in args.symbols.split(",") if s.strip())
    names = tuple(n.strip() for n in args.strategies.split(",") if n.strip())
    return PaperRunConfig(
        symbols=symbols,
        timeframe=Timeframe(args.timeframe),
        capital=args.capital,
        strategies=names,
        params=parse_values(args.params),
        risk=RiskConfig(taker_fee_rate=args.taker_fee),
        maker_fee=args.maker_fee,
        slippage_bps=args.slippage_bps,
        latency_ms=args.latency_ms,
        max_stop_blind_seconds=args.max_stop_blind_seconds,
        allow_open_calendar=args.allow_open_calendar,
        history_window=args.history_window,
        heartbeat_seconds=args.heartbeat_seconds,
        equity_seconds=args.equity_seconds,
        reconcile_seconds=max(1, round(args.reconcile_minutes * 60)),
        selector=SelectorSettings(enabled=args.selector, recalc_hours=args.selector_recalc_hours),
    )


def load_calendar(path: Path) -> EventCalendar:
    if not path.is_file():
        log.error("event_calendar_missing", path=str(path), effect="sem cobertura: entradas recusadas")
        return EventCalendar()
    return EventCalendar.load(path)


def install_signal_handlers(loop: asyncio.AbstractEventLoop, robot: PaperRobot) -> None:
    """SIGINT/SIGTERM (e Ctrl+Break no Windows) viram parada ordenada; nunca remove stops."""

    def handler(signum: int, _frame: Any) -> None:
        loop.call_soon_threadsafe(robot.request_stop, f"sinal {signal.Signals(signum).name}")

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if (number := getattr(signal, name, None)) is not None:
            signal.signal(number, handler)


async def run_start(args: argparse.Namespace) -> int:
    settings = load_settings()
    configure_logging(args.log_level or settings.log_level, json=args.json_logs)
    base_config = build_config(args)
    run_id = _run_id(args, create=True)
    paths = RunPaths(args.data_dir, run_id)
    paths.ensure()
    instance = InstanceLock(paths.lock)
    try:
        instance.acquire()
    except AlreadyRunningError as error:
        print(f"erro: {error}", file=sys.stderr)
        return EXIT_USAGE
    write_latest(args.data_dir, run_id)
    ws_url = args.ws_url or settings.paper_ws_url
    sha, dirty = git_version()
    version = f"{sha[:12] if sha else 'desconhecida'}{'+dirty' if dirty else ''}"
    db_engine = create_engine(settings.database_url)
    sessions = create_session_factory(db_engine)
    redis_url = with_redis_password(settings.redis_url, resolve_secrets_dir())
    signing_key = load_command_key(resolve_secrets_dir())
    bus: EventBus = NullEventBus() if args.no_redis else RedisEventBus(redis_url, signing_key=signing_key)
    market = BinanceMarketData(rest_url=settings.binance_rest_url, ws_url=ws_url)
    signed, signed_source = _signed_source(args, redis_url)
    try:
        versioned = await apply_versioned_config(
            sessions, base_config, run_id, enabled=not args.no_versioned_config
        )
    except Exception as error:
        log.critical("versioned_config_failed", error=scrub_text(repr(error)))
        instance.release()
        await bus.close()
        await market.aclose()
        await db_engine.dispose()
        return EXIT_FATAL
    config = versioned.config
    strategies = versioned.strategies or build_strategies(config.strategies, config.params)
    if config.selector.enabled:
        names = [n for n in config.strategies if n in STRATEGY_REGISTRY] or list(STRATEGY_REGISTRY)
        config = config.model_copy(update={"strategies": tuple(names)})
        strategies = [STRATEGY_REGISTRY[n]() for n in names]
    paths.config.write_text(config.model_dump_json(indent=2), encoding="utf-8")
    robot = PaperRobot(
        config,
        run_id=run_id,
        journal=SqlJournal(sessions, run_id),
        bus=bus,
        market=market,
        strategies=strategies,
        score_store=SqlScoreStore(sessions) if config.selector.enabled else None,
        backtest_samples=(
            read_samples(samples_path(args.selector_samples_dir, config.timeframe.value))
            if config.selector.enabled
            else ()
        ),
        signed_commands=signed,
        audit_sink=SqlAuditSink(sessions),
        alerts=AlertDispatcher(
            build_notifier(NotifySettings(), resolve_secrets_dir()),
            run_id=run_id,
            cooldown_seconds=NotifySettings().cooldown_seconds,
        ),
        config_source=versioned.source,
        rules=_rules(config.symbols),
        calendar=load_calendar(args.calendar),
        clock=SystemClock(),
        commands=CommandQueue(paths.commands),
        paths=paths,
        code_version=version,
        max_runtime=timedelta(minutes=args.duration_minutes) if args.duration_minutes else None,
        liquidate_on_emergency=args.emergency_liquidate,
    )
    started = False
    code = EXIT_OK
    try:
        async with db_engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        async with database_locks(db_engine, run_id, config.symbols):
            try:
                install_signal_handlers(asyncio.get_running_loop(), robot)
                print(
                    f"run_id={run_id} ws={ws_url} simbolos={','.join(config.symbols)} "
                    f"tf={config.timeframe.value}"
                )
                await robot.start()
                started = True
                await robot.run()
            except Exception as error:
                log.critical("paper_fatal", error=scrub_text(repr(error)), exc_info=True)
                code = EXIT_FATAL
            finally:
                if started:
                    try:
                        await robot.shutdown()
                    except Exception as error:
                        log.critical("paper_shutdown_failed", error=scrub_text(repr(error)))
                        code = EXIT_FATAL
    except AlreadyRunningError as error:
        print(f"erro: {error}", file=sys.stderr)
        code = EXIT_USAGE
    except Exception as error:
        log.critical("paper_fatal", error=scrub_text(repr(error)), exc_info=True)
        code = EXIT_FATAL
    finally:
        instance.release()
        await bus.close()
        if signed_source is not None:
            await signed_source.close()
        await market.aclose()
        await db_engine.dispose()
    print(f"run {run_id} encerrado (codigo {code}); posicoes abertas mantem os stops no estado salvo")
    return code


def _signed_source(
    args: argparse.Namespace, redis_url: str
) -> tuple[SignedCommandSource | None, RedisCommandSource | None]:
    """Canal assinado (Redis) so liga com Redis ativo E chave compartilhada valida em SECRETS_DIR."""
    if args.no_redis or args.no_signed_commands:
        return None, None
    key = load_command_key(resolve_secrets_dir())
    if key is None:
        log.warning("signed_commands_disabled", reason="command_hmac_key ausente ou curta em SECRETS_DIR")
        return None, None
    source = RedisCommandSource.from_url(redis_url, key)
    return source, source


def run_command(args: argparse.Namespace) -> int:
    run_id = _run_id(args)
    paths = RunPaths(args.data_dir, run_id)
    if not paths.root.is_dir():
        print(f"run {run_id} desconhecido em {paths.root}", file=sys.stderr)
        return EXIT_USAGE
    name = CommandName(args.command)
    liquidate = bool(getattr(args, "liquidate", False))
    strict = name in (CommandName.RESUME, CommandName.REACTIVATE) or (
        name is CommandName.EMERGENCY_STOP and liquidate
    )
    if strict and not (args.operator and args.reason):
        raise ValueError(f"{args.command}{' --liquidate' if liquidate else ''} exige --operator e --reason")
    command = Command(
        name=name,
        operator=args.operator or getpass.getuser(),
        reason=scrub_text(args.reason or "solicitado pelo operador"),
        liquidate=liquidate,
        issued_at=datetime.now(UTC),
    )
    path = CommandQueue(paths.commands).send(command)
    print(f"comando {command.name.value} enviado ao run {run_id} ({path.name}); o robo o aplica em ate ~1 s")
    status = read_status(paths.status)
    if status is None or _stale(status):
        print(
            "AVISO: o robo nao parece estar em execucao; comandos expiram em "
            f"{int(COMMAND_TTL.total_seconds() // 60)} min e os emitidos antes da partida sao descartados "
            "(exceto emergency-stop; com --liquidate vale "
            f"{int(LIQUIDATE_TTL.total_seconds() // 60)} min)"
        )
    return EXIT_OK


def _stale(status: dict[str, Any]) -> bool:
    try:
        beat = datetime.fromisoformat(str(status["heartbeat_at"]))
    except (KeyError, ValueError):
        return True
    return datetime.now(UTC) - beat > _STATUS_STALE or status.get("state") == "STOPPED"


async def run_status(args: argparse.Namespace) -> int:
    run_id = _run_id(args)
    paths = RunPaths(args.data_dir, run_id)
    status = read_status(paths.status)
    print(f"run {run_id}")
    if status is None:
        print("  sem status local (o robo nunca rodou aqui?)")
    else:
        alive = "PARADO/sem heartbeat recente" if _stale(status) else "em execucao"
        print(f"  processo: {alive}; estado {status.get('state')}; heartbeat {status.get('heartbeat_at')}")
        for key in (
            "equity",
            "realized_equity",
            "drawdown",
            "day_pnl",
            "stops_blind",
            "paused",
            "pause_reason",
            "halted",
            "halt_reason",
            "daily_pause_until",
            "target_reached_on",
            "pair_pause_until",
            "open_positions",
            "pending_entries",
            "last_candle",
            "redis_healthy",
            "allow_open_calendar",
        ):
            print(f"  {key}: {status.get(key)}")
    settings = load_settings()
    db_engine = create_engine(settings.database_url)
    try:
        recovery = await load_recovery_state(create_session_factory(db_engine), run_id)
        if recovery.run is None:
            print("  diario: run inexistente")
        else:
            state = recovery.risk_state
            print(
                f"  diario: iniciado {recovery.run.started_at.isoformat()} versao {recovery.run.code_version}"
            )
            if state is not None:
                print(
                    f"  risco: equity {state.equity} pico {state.peak_equity} "
                    f"halted={state.halted} paused={state.paused}"
                )
            for position in recovery.open_positions:
                print(f"  posicao: {position.symbol} qtd {position.quantity} stop {position.stop}")
    finally:
        await db_engine.dispose()
    return EXIT_OK


async def run_report(args: argparse.Namespace) -> int:
    settings: Settings = load_settings()
    run_id = _run_id(args)
    now = datetime.now(UTC)
    day = date.fromisoformat(args.date) if args.date else now.date()
    db_engine = create_engine(settings.database_url)
    try:
        inputs = await collect_inputs(
            create_session_factory(db_engine),
            run_id,
            now=now,
            compare=not args.no_compare,
            cache_dir=args.cache_dir,
        )
    except UnknownRunError as error:
        print(str(error), file=sys.stderr)
        return EXIT_USAGE
    finally:
        await db_engine.dispose()
    out = args.out / run_id
    daily = write_report(out, f"daily-{day.isoformat()}", build_daily(inputs, day))
    cumulative_report = build_cumulative(inputs)
    cumulative = write_report(out, "cumulative", cumulative_report)
    gate = cumulative_report["live_gate"]
    print(f"relatorio diario: {daily[1]}\nrelatorio acumulado: {cumulative[1]}")
    print(
        f"trades {cumulative_report['metrics']['trades']} | dias de operacao "
        f"{cumulative_report['metrics']['operating_days']} | Live {gate['verdict']}"
    )
    for note in cumulative_report["notes"]:
        print(f"nota: {note}")
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        match args.command:
            case "start":
                code = asyncio.run(run_start(args))
            case "status":
                code = asyncio.run(run_status(args))
            case "report":
                code = asyncio.run(run_report(args))
            case _:
                code = run_command(args)
    except ValueError as error:
        print(f"erro: {error}", file=sys.stderr)
        code = EXIT_USAGE
    raise SystemExit(code)


if __name__ == "__main__":
    main()
