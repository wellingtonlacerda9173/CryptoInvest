import json

from pydantic import ValidationError

from engine.application.ai.models import PROMPT_VERSION, AnalystOpinion, AnalystVerdict

MAX_RAW_CHARS = 4000


def parse_opinion(raw: str, model: str, prompt_version: str = PROMPT_VERSION) -> AnalystOpinion | None:
    """So aceita UM objeto JSON puro que cumpra o schema. Texto livre, JSON no meio de texto, cerca de
    codigo, chaves a mais, a menos ou DUPLICADAS, tipos errados (`confidence` precisa ser numero JSON: bool e
    texto sao recusados; inteiros 0 e 1 valem como 0.0 e 1.0) ou justificativa longa demais => None."""
    if not isinstance(raw, str) or len(raw) > MAX_RAW_CHARS:
        return None
    try:
        data = json.loads(raw.strip(), parse_constant=_reject_constant, object_pairs_hook=_no_duplicates)
        verdict = AnalystVerdict.model_validate(data, strict=True)
        return AnalystOpinion(
            verdict=verdict.verdict,
            confidence=verdict.confidence,
            reason=verdict.reason,
            model=model,
            prompt_version=prompt_version,
        )
    except (ValueError, ValidationError):
        return None


def _reject_constant(name: str) -> float:
    raise ValueError(f"constante JSON nao aceita: {name}")


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("chave duplicada no JSON")
    return dict(pairs)
