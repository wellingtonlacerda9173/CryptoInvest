from typing import Protocol

from engine.application.ai.models import AnalystOpinion, AnalystRequest


class MarketAnalyst(Protocol):
    async def analyze(self, request: AnalystRequest, *, decision_key: str = "") -> AnalystOpinion | None:
        """Parecer sobre um sinal JA aprovado pelo gate; None = sem parecer (o robo segue como sem IA).

        Nunca levanta por indisponibilidade, atraso, orcamento ou resposta invalida. `decision_key` e so
        para auditoria local: nao faz parte do que vai ao modelo.
        """
        ...
