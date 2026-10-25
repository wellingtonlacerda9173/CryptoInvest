from engine.application.ai.models import AnalystOpinion, CombinedResult


def combine(
    signal_confidence: float, opinion: AnalystOpinion | None, *, min_confidence: float = 0.0
) -> CombinedResult:
    """A IA so reduz: veto bloqueia, aprovacao pode baixar a confianca, nunca eleva nada.

    Sem parecer, ou com o sinal ja abaixo do minimo (o que a IA nao causou), o resultado e o de sem IA.
    """
    if opinion is None:
        return CombinedResult(approved=True, confidence=signal_confidence)
    if opinion.verdict == "veto":
        return CombinedResult(approved=False, confidence=0.0, code="ai_veto")
    confidence = min(signal_confidence, opinion.confidence)
    if confidence < min_confidence <= signal_confidence:
        return CombinedResult(approved=False, confidence=confidence, code="ai_low_confidence")
    return CombinedResult(approved=True, confidence=confidence)
