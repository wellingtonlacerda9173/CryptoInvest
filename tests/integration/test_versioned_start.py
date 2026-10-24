"""Inicio do runner com a configuracao versionada (banco real): a ultima versao manda, o hash e registrado e,
com posicao aberta, nenhum aumento de risco entra."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from engine.application.strategies import EmaCrossTrend
from engine.application.trading_config import ConfigDocument, default_document
from engine.infrastructure.persistence.config_repository import (
    ConfigRepository,
    load_latest,
    load_run_document,
)
from engine.infrastructure.persistence.models import AuditLogRow, PositionRow, RunRow
from engine.runner.paper import build_parser
from engine.runner.paper_config import PaperRunConfig
from engine.runner.paper_versioned import apply_versioned_config
from tests.integration.api.runner_support import module_db_url as module_db_url
from tests.integration.api.runner_support import module_sessions as module_sessions

pytestmark = pytest.mark.integration

D = Decimal
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def base_config() -> PaperRunConfig:
    return PaperRunConfig(capital=D(5000), symbols=("BTC/USDT",))


def edit(doc: ConfigDocument, section: str, **values: Any) -> ConfigDocument:
    return doc.with_section(section, {**doc.dump()[section], **values})  # type: ignore[arg-type]


async def save(sessions: async_sessionmaker[AsyncSession], doc: ConfigDocument, reason: str = "teste") -> int:
    async with sessions.begin() as db:
        repo = ConfigRepository(db)
        latest = await repo.latest()
        version = await repo.append(
            doc,
            actor="ana",
            reason=reason,
            activation="next_start",
            risk_increase=False,
            sections=["risk"],
            diff=[],
            expected_parent=latest.id if latest else None,
        )
    return version.id


async def test_without_a_saved_version_the_cli_configuration_is_used(module_sessions: Any) -> None:
    start = await apply_versioned_config(module_sessions, base_config(), "paper-x")
    assert start.config == base_config() and start.strategies is None
    assert start.source == {"origin": "cli", "reason": "sem versão gravada"}


async def test_the_latest_version_defines_pairs_strategies_risk_and_the_run_hash(
    module_sessions: Any,
) -> None:
    doc = edit(default_document(), "risk", risk_per_trade="0.004", max_open_positions=1)
    doc = doc.with_section("pairs", {"symbols": ["BTC/USDT", "ETH/USDT"], "timeframe": "4h"})
    doc = edit(doc, "strategies", elephant_enabled=False)
    version_id = await save(module_sessions, doc, "primeira versao")

    base = base_config()
    start = await apply_versioned_config(module_sessions, base, "paper-x")
    config = start.config
    assert config.symbols == ("BTC/USDT", "ETH/USDT") and config.timeframe.value == "4h"
    assert config.strategies == ("ema",) and config.risk == doc.risk and config.capital == D(5000)
    assert config.document_hash == doc.digest() and config.config_hash() != base.config_hash()
    assert [type(s) for s in start.strategies or []] == [EmaCrossTrend]
    assert start.source["origin"] == "versioned" and start.source["version_id"] == version_id
    assert (
        start.source["effective_hash"] == doc.digest()
        and start.source["restricted_by_open_positions"] is False
    )
    assert ConfigDocument.model_validate(start.source["document"]).digest() == doc.digest()

    newer = edit(doc, "risk", risk_per_trade="0.003")
    await save(module_sessions, newer, "reduz o risco")
    again = await apply_versioned_config(module_sessions, base, "paper-x")
    assert (
        again.config.risk.risk_per_trade == D("0.003") and again.config.config_hash() != config.config_hash()
    )


async def test_the_versioned_config_can_be_turned_off_by_the_cli_flag(module_sessions: Any) -> None:
    start = await apply_versioned_config(module_sessions, base_config(), "paper-x", enabled=False)
    assert start.config == base_config() and start.source["origin"] == "cli"
    args = build_parser().parse_args(["start", "--no-versioned-config", "--no-signed-commands"])
    assert args.no_versioned_config is True and args.no_signed_commands is True
    assert build_parser().parse_args(["start"]).no_versioned_config is False


async def seed_open_run(
    sessions: async_sessionmaker[AsyncSession], run_id: str, running: ConfigDocument | None
) -> None:
    async with sessions.begin() as db:
        db.add(RunRow(run_id=run_id, mode="paper", started_at=NOW, config_hash="h" * 32, code_version="x"))
        await db.flush()
        config: dict[str, Any] = {"config": {"capital": "5000"}}
        if running is not None:
            config["config_source"] = {"document": running.dump()}
        db.add(AuditLogRow(actor="system", action="paper_start", entity=run_id, payload={"config": config}))
        db.add(
            PositionRow(
                run_id=run_id,
                symbol="BTC/USDT",
                entry_price=D(100),
                initial_quantity=D(1),
                quantity=D(1),
                initial_stop=D(95),
                stop=D(97),
                take_profit=D(110),
                highest_price=D(104),
                entry_fee=D("0.1"),
                breakeven_reached=False,
                partial_done=False,
                realized_pnl=D(0),
                opened_at=NOW - timedelta(hours=1),
            )
        )


async def test_resuming_with_an_open_position_takes_reductions_but_never_increases(
    module_sessions: Any,
) -> None:
    latest = await load_latest(module_sessions)
    assert latest is not None
    running = latest.document
    risky = edit(running, "risk", risk_per_trade="0.009", max_drawdown="0.04", max_open_positions=1)
    risky = risky.with_section("pairs", {"symbols": ["BTC/USDT", "ETH/USDT", "SOL/USDT"], "timeframe": "4h"})
    await save(module_sessions, risky, "tenta aumentar")
    await seed_open_run(module_sessions, "paper-open", running)
    assert (await load_run_document(module_sessions, "paper-open")) is not None

    start = await apply_versioned_config(module_sessions, base_config(), "paper-open")
    config = start.config
    assert config.risk.risk_per_trade == running.risk.risk_per_trade
    assert config.risk.max_drawdown == D("0.04") and config.risk.max_open_positions == 1
    assert config.symbols == running.pairs.symbols and config.timeframe == running.pairs.timeframe
    assert start.source["restricted_by_open_positions"] is True

    fresh = await apply_versioned_config(module_sessions, base_config(), "paper-another-run")
    assert fresh.config.risk.risk_per_trade == D("0.009") and fresh.config.symbols == tuple(
        risky.pairs.symbols
    )
    assert fresh.source["restricted_by_open_positions"] is False


async def test_an_open_position_without_the_previous_document_falls_back_to_the_cli(
    module_sessions: Any,
) -> None:
    await seed_open_run(module_sessions, "paper-open-cli", None)
    start = await apply_versioned_config(module_sessions, base_config(), "paper-open-cli")
    assert start.config == base_config() and start.strategies is None
    assert start.source["origin"] == "cli" and "posição aberta" in start.source["reason"]
