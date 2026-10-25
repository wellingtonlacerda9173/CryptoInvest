"""Evidencias de QA da Fase 6 (corridas do lock liberado durante a espera da IA)."""

import asyncio

from engine.application.ai import AnalystOpinion, AnalystRequest
from tests.unit.ai.support import opinion
from tests.unit.runner.fakes import Harness, candle_at
from tests.unit.runner.test_paper_ai import SIGNAL_CANDLE, ai_config


class BlockingAnalyst:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def analyze(self, request: AnalystRequest, *, decision_key: str = "") -> AnalystOpinion | None:
        self.started.set()
        await self.release.wait()
        return opinion("approve", 0.9, "ok")


async def test_cancel_during_ai_wait_does_not_deadlock_the_shutdown_sequence() -> None:
    analyst = BlockingAnalyst()
    h = await Harness(cfg=ai_config(), analyst=analyst).start()
    candle = candle_at(29, *SIGNAL_CANDLE)
    h.clock.set(candle.close_time)
    await h.tick()
    feeder = asyncio.create_task(h.robot.on_candle(candle))
    await asyncio.wait_for(analyst.started.wait(), 2)

    async def run_finally_like() -> None:
        # espelha PaperRobot.run(): cancela as tarefas COM o lock e espera por elas
        async with h.robot._lock:
            feeder.cancel()
            await asyncio.gather(feeder, return_exceptions=True)

    await asyncio.wait_for(run_finally_like(), 3)


def test_orcamento_da_ia_nao_deve_mudar_a_identidade_do_run() -> None:
    from decimal import Decimal

    from engine.application.trading_config.document import AiConfig, ConfigDocument
    from tests.unit.runner.fakes import config

    base = ConfigDocument()
    other = ConfigDocument(ai=AiConfig(monthly_budget_usd=Decimal(35), timeout_seconds=10.0))
    first = config(document_hash=base.identity_digest()).config_hash()
    second = config(document_hash=other.identity_digest()).config_hash()
    assert first == second


async def test_stop_between_collection_and_ai_answer_drops_the_entry_and_keeps_the_lock_free() -> None:
    analyst = BlockingAnalyst()
    h = await Harness(cfg=ai_config(), analyst=analyst).start()
    candle = candle_at(29, *SIGNAL_CANDLE)
    h.clock.set(candle.close_time)
    await h.tick()
    feeder = asyncio.create_task(h.robot.on_candle(candle))
    await asyncio.wait_for(analyst.started.wait(), 2)
    h.robot.request_stop("teste")
    analyst.release.set()
    await asyncio.wait_for(feeder, 3)

    assert not h.robot._lock.locked()
    assert not h.robot._pending
    await asyncio.wait_for(h.robot.shutdown("teste"), 3)


def test_digest_do_documento_padrao_e_o_legado_e_a_ia_so_muda_o_digest_da_versao() -> None:
    from decimal import Decimal

    from engine.application.trading_config.document import AiConfig, ConfigDocument

    legacy = {
        "pairs": ConfigDocument().dump()["pairs"],
        "strategies": ConfigDocument().dump()["strategies"],
        "risk": ConfigDocument().dump()["risk"],
        "ai": {"enabled": False, "model": "claude-sonnet-5-5"},
    }
    from engine.application.trading_config.document import canonical_hash

    assert ConfigDocument().digest() == canonical_hash(legacy)
    changed = ConfigDocument(ai=AiConfig(monthly_budget_usd=Decimal(35)))
    assert changed.digest() != ConfigDocument().digest()
    assert changed.identity_digest() == ConfigDocument().identity_digest() == ConfigDocument().digest()


def test_run_antigo_retoma_com_a_ia_ligada_e_outro_orcamento() -> None:
    from decimal import Decimal

    from engine.application.trading_config.document import AiConfig, ConfigDocument
    from tests.unit.runner.fakes import config

    stored = config(document_hash=ConfigDocument().digest()).config_hash()
    new_ai = AiConfig(
        enabled=True, model="claude-sonnet-5-5", monthly_budget_usd=Decimal(50), timeout_seconds=3.0
    )
    resumed = config(
        document_hash=ConfigDocument(ai=new_ai).identity_digest(), ai=new_ai, ai_min_confidence=0.9
    ).config_hash()
    assert resumed == stored
