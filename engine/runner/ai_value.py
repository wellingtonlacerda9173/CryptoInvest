"""Valor da IA: `python -m engine.runner.ai_value [--days 30]`. So leitura do banco; nunca chama a IA.

Para cada sinal vetado pela IA simula o que teria rendido (mesmas regras do backtest, dimensionamento real) e
compara com o que a IA aprovou e foi executado. O veredito e honesto: amostra pequena nao conclui nada.
"""

import argparse
import asyncio
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from engine.application.ai.value import AiValueReport, build_report, simulate_vetoes
from engine.application.backtest.settings import BacktestSettings
from engine.domain.risk import RiskConfig
from engine.infrastructure.config.logging import scrub_text
from engine.infrastructure.config.settings import load_settings
from engine.infrastructure.persistence.ai_value_repository import AiValueRepository
from engine.infrastructure.persistence.config_repository import ConfigRepository
from engine.infrastructure.persistence.session import create_engine, create_session_factory

DEFAULT_DAYS = 30
MAX_DAYS = 365


async def measure_ai_value(
    sessions: async_sessionmaker[AsyncSession], *, days: int = DEFAULT_DAYS, now: datetime | None = None
) -> AiValueReport:
    end = now or datetime.now(UTC)
    start = end - timedelta(days=days)
    async with sessions() as session:
        latest = await ConfigRepository(session).latest()
        settings = BacktestSettings(risk=latest.document.risk if latest is not None else RiskConfig())
        repo = AiValueRepository(session, settings)
        vetoes = await repo.vetoes(start, end)
        approved = await repo.approved(start, end)
        calls, failed, cost = await repo.usage(start, end)
    return build_report(
        simulate_vetoes(vetoes, settings),
        approved,
        calls=calls,
        failed_calls=failed,
        cost_usd=cost,
        start=start,
        end=end,
    )


def render(report: AiValueReport) -> str:
    if report.verdict == "no_activity":
        return f"{report.verdict_text}\n"
    return (
        f"Período: {report.period_start:%d/%m/%Y} a {report.period_end:%d/%m/%Y}\n"
        f"Chamadas: {report.calls} ({report.failed_calls} com falha) | custo US$ {report.cost_usd:.4f}\n"
        f"Vetos: {report.vetoes} ({report.simulated} simulados, {report.not_filled} sem entrada, "
        f"{report.unresolved} em aberto, {report.not_simulated} sem dados)\n"
        f"Perdas evitadas: {report.avoided_loss:.2f} | ganhos barrados: {report.missed_gain:.2f} | "
        f"saldo: {report.net_avoided:+.2f} | falsos vetos: {report.false_vetoes}\n"
        f"Aprovadas e executadas: {report.approved_trades} ({report.approved_wins} ganhos, "
        f"{report.approved_losses} perdas, perdas {report.approved_loss_pnl:+.2f}, "
        f"resultado {report.approved_pnl:+.2f})\n"
        f"Valor líquido (saldo dos vetos - custo): {report.net_value_usd:+.2f}\n"
        f"Veredito: {report.verdict_text}\n"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m engine.runner.ai_value", description=__doc__)
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    return parser


async def main_async(args: argparse.Namespace) -> int:
    if not 1 <= args.days <= MAX_DAYS:
        print(f"erro: --days deve estar entre 1 e {MAX_DAYS}", file=sys.stderr)
        return 2
    engine = create_engine(load_settings().database_url)
    try:
        report = await measure_ai_value(create_session_factory(engine), days=args.days)
    except Exception as error:
        print(f"erro: {scrub_text(repr(error))}", file=sys.stderr)
        return 1
    finally:
        await engine.dispose()
    print(render(report), end="")
    return 0


def main(argv: Sequence[str] | None = None) -> None:
    raise SystemExit(asyncio.run(main_async(build_parser().parse_args(argv))))


if __name__ == "__main__":
    main()
