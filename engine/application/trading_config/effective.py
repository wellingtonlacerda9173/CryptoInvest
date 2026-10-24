"""Configuração efetiva de um (re)início: o que o documento versionado pode mudar com o robô em campo."""

from engine.application.trading_config.document import ConfigDocument
from engine.application.trading_config.risk_delta import limit_to_reductions


def effective_document(
    latest: ConfigDocument, running: ConfigDocument | None, *, open_positions: bool
) -> ConfigDocument:
    """Sem posição aberta vale a última versão inteira (aumentos de risco entram aqui).

    Com posição aberta (retomada do mesmo run) NADA que aumente risco vale: pares e estratégias ficam como
    estavam, o risco só aceita reduções e a IA (que não toca ordens) acompanha a versão nova. Os aumentos
    esperam o próximo início sem posição.
    """
    if not open_positions or running is None:
        return latest
    return ConfigDocument(
        pairs=running.pairs,
        strategies=running.strategies,
        risk=limit_to_reductions(running.risk, latest.risk),
        ai=latest.ai,
    )
