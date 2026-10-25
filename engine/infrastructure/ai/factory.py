from dataclasses import dataclass

import structlog

from engine.application.ai import MarketAnalyst
from engine.application.ai.ledger import AiCallStore
from engine.application.trading_config.document import AiConfig
from engine.infrastructure.ai.analyst import AnthropicAnalyst
from engine.infrastructure.ai.pricing import DEFAULT_PRICES, Price
from engine.infrastructure.config.settings import Settings

log = structlog.get_logger()

DISABLED = "desligada"
NO_KEY = "sem chave: IA desligada"
NO_PACKAGE = "pacote anthropic ausente: IA desligada"


@dataclass(frozen=True, slots=True)
class AnalystBuild:
    analyst: MarketAnalyst | None
    status: str


def build_analyst(
    config: AiConfig, settings: Settings, store: AiCallStore, prices: dict[str, Price] | None = None
) -> AnalystBuild:
    """Analista real so com `ai.enabled`, chave no arquivo secreto e o pacote instalado; senao o robo opera
    exatamente como sem IA e o motivo vai para o status."""
    if not config.enabled:
        return AnalystBuild(None, DISABLED)
    key = settings.anthropic_api_key
    if key is None or not key.get_secret_value().strip():
        log.warning("ai_disabled_without_key", effect="IA desligada: o robo opera como sem IA")
        return AnalystBuild(None, NO_KEY)
    try:
        from engine.infrastructure.ai.client import AnthropicClient

        client = AnthropicClient(key)
    except ImportError:
        log.warning("ai_disabled_without_package", hint="uv sync --extra ai")
        return AnalystBuild(None, NO_PACKAGE)
    analyst = AnthropicAnalyst(
        client,
        store,
        model=config.model,
        monthly_budget_usd=config.monthly_budget_usd,
        timeout_seconds=config.timeout_seconds,
        prices=prices or DEFAULT_PRICES,
    )
    return AnalystBuild(analyst, "ligada")
