"""Diario de licoes: `python -m engine.runner.lessons [--period-days 7] [--no-ai]`.

Reavalia os scores (deterministico, sem IA, snapshot novo append-only) e grava um relatorio de licoes
PENDENTE: nada entra no prompt da IA sem aprovacao de um admin no painel. Sem IA (desligada, sem chave, sem
orcamento, circuito aberto, resposta invalida) o relatorio sai so com estatisticas.
"""

import argparse
import asyncio
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from engine.application.ai.lesson_job import (
    DEFAULT_PERIOD_DAYS,
    LessonJobResult,
    LessonWriter,
    NewLesson,
    Origin,
    run_lesson_job,
)
from engine.application.selector import SelectorSettings, StrategyScore
from engine.infrastructure.ai.analyst import AnthropicAnalyst
from engine.infrastructure.ai.factory import build_analyst
from engine.infrastructure.ai.lessons import AnthropicLessonWriter
from engine.infrastructure.ai.pricing import load_prices
from engine.infrastructure.config.logging import configure_logging, scrub_text
from engine.infrastructure.config.settings import Settings, load_settings
from engine.infrastructure.persistence.ai_call_repository import SqlAiCallStore
from engine.infrastructure.persistence.config_repository import ConfigRepository
from engine.infrastructure.persistence.lesson_repository import SqlLessonStore
from engine.infrastructure.persistence.session import create_engine, create_session_factory
from engine.runner.selector_store import (
    DEFAULT_SAMPLES_DIR,
    SqlScoreStore,
    read_samples,
    samples_path,
)

log = structlog.get_logger()

AI_PRICES_FILE = Path("config/ai_prices.json")
EXIT_OK = 0
EXIT_FAILED = 1


async def _writer(
    sessions: async_sessionmaker[AsyncSession], settings: Settings, analyst: object | None, use_ai: bool
) -> LessonWriter | None:
    if not use_ai:
        return None
    # `use_ai` sem analista = chamada sob demanda (CLI/endpoint): monta o analista do documento versionado,
    # sempre sobre o mesmo livro/orcamento. O job interno do robo passa `use_ai=False` com a IA desligada.
    if isinstance(analyst, AnthropicAnalyst):
        return AnthropicLessonWriter(analyst)
    async with sessions() as session:
        latest = await ConfigRepository(session).latest()
    if latest is None:
        return None
    built = build_analyst(latest.document.ai, settings, SqlAiCallStore(sessions), load_prices(AI_PRICES_FILE))
    return AnthropicLessonWriter(built.analyst) if isinstance(built.analyst, AnthropicAnalyst) else None


async def generate_lesson(
    sessions: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    timeframe: str,
    symbols: Sequence[str] | None = None,
    samples_dir: Path = DEFAULT_SAMPLES_DIR,
    period_days: int = DEFAULT_PERIOD_DAYS,
    lookback_days: int | None = None,
    use_ai: bool = True,
    analyst: object | None = None,
    now: datetime | None = None,
) -> LessonJobResult:
    moment = now or datetime.now(UTC)
    scores_store, lessons_store = SqlScoreStore(sessions), SqlLessonStore(sessions)
    journal = await scores_store.journal_samples(symbols or ())
    wanted = None if not symbols else set(symbols)
    journal = [s for s in journal if wanted is None or s.symbol in wanted]
    backtest = [
        s for s in read_samples(samples_path(samples_dir, timeframe)) if wanted is None or s.symbol in wanted
    ]
    decisions = await lessons_store.skipped_counts(moment - timedelta(days=period_days), moment)

    async def publish(scores: Sequence[StrategyScore], origin: Origin) -> str:
        return await scores_store.publish(scores, timeframe=timeframe, origin=origin, calculated_at=moment)

    async def save(lesson: NewLesson) -> str:
        return await lessons_store.add(lesson)

    return await run_lesson_job(
        journal=journal,
        backtest_samples=backtest,
        decisions=decisions,
        settings=SelectorSettings(lookback_days=lookback_days),
        now=moment,
        publish=publish,
        save=save,
        writer=await _writer(sessions, settings, analyst, use_ai),
        period_days=period_days,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m engine.runner.lessons", description=__doc__)
    parser.add_argument("--timeframe", default="1h")
    parser.add_argument("--symbols", default="", help="filtra por pares (BTC/USDT,ETH/USDT); vazio = todos")
    parser.add_argument("--period-days", type=int, default=DEFAULT_PERIOD_DAYS)
    parser.add_argument("--lookback-days", type=int, default=None)
    parser.add_argument("--samples-dir", type=Path, default=DEFAULT_SAMPLES_DIR)
    parser.add_argument("--no-ai", action="store_true", help="relatorio so com estatisticas")
    parser.add_argument("--log-level", default=None)
    return parser


async def main_async(args: argparse.Namespace) -> int:
    settings = load_settings()
    configure_logging(args.log_level or settings.log_level, json=False)
    symbols = tuple(s.strip().upper() for s in args.symbols.split(",") if s.strip())
    engine = create_engine(settings.database_url)
    try:
        result = await generate_lesson(
            create_session_factory(engine),
            settings,
            timeframe=args.timeframe,
            symbols=symbols or None,
            samples_dir=args.samples_dir,
            period_days=args.period_days,
            lookback_days=args.lookback_days,
            use_ai=not args.no_ai,
        )
    except Exception as error:
        print(f"erro: {scrub_text(repr(error))}", file=sys.stderr)
        return EXIT_FAILED
    finally:
        await engine.dispose()
    print(
        f"licao {result.lesson_id} ({result.source}) | {result.trades} operacoes | "
        f"{result.scores} scores recalculados | pendente de aprovacao"
    )
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> None:
    raise SystemExit(asyncio.run(main_async(build_parser().parse_args(argv))))


if __name__ == "__main__":
    main()
