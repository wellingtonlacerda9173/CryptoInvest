from collections.abc import Iterable, Mapping
from enum import StrEnum
from typing import Final


class Family(StrEnum):
    VOLUME = "volume"
    TREND = "tendencia"
    MOMENTUM = "momentum"
    STRUCTURE = "estrutura"


# Indicadores da mesma familia medem a mesma coisa (ex.: DI+, EMA50 e HTF): contam uma vez so.
CONFLUENCE_FAMILIES: Final[Mapping[str, Family]] = {
    "volume_forte": Family.VOLUME,
    "volume_acima_da_media": Family.VOLUME,
    "preco_acima_da_ema_de_tendencia": Family.TREND,
    "dominio_comprador_di": Family.TREND,
    "tendencia_superior_de_alta": Family.TREND,
    "rsi_com_espaco": Family.MOMENTUM,
    "rsi_em_zona_de_forca": Family.MOMENTUM,
    "sem_exaustao": Family.STRUCTURE,
    "fechamento_acima_da_maxima_recente": Family.STRUCTURE,
}

# Pre-requisitos obrigatorios do setup: nao sao confluencias e nunca entram na contagem.
TRIGGERS: Final = frozenset(
    {"barra_elefante_alta", "rompimento_de_resistencia", "cruzamento_ema_para_cima", "adx_em_tendencia"}
)


def independent_families(confluences: Iterable[str]) -> frozenset[Family]:
    return frozenset(f for name in confluences if (f := CONFLUENCE_FAMILIES.get(name)) is not None)


def score_confluences(checks: Mapping[str, bool]) -> tuple[tuple[str, ...], frozenset[Family], float]:
    """(confluencias confirmadas, familias confirmadas, confianca = media do acerto por familia)."""
    hits = tuple(name for name, ok in checks.items() if ok)
    by_family: dict[Family, list[bool]] = {}
    for name, ok in checks.items():
        by_family.setdefault(CONFLUENCE_FAMILIES[name], []).append(ok)
    confidence = sum(sum(v) / len(v) for v in by_family.values()) / len(by_family) if by_family else 0.0
    return hits, independent_families(hits), confidence
