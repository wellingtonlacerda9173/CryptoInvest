"""Higiene de texto vindo de fora (mercado, usuario, modelo): nada disso e confiavel, nunca vira instrucao.

Isto e HEURISTICA de reducao de superficie, NAO uma fronteira de seguranca: nenhuma lista de padroes pega
todo texto adversarial. A seguranca vem da arquitetura: o texto segue como DADO dentro de JSON, a IA so pode
vetar/reduzir confianca (nunca aumentar risco nem criar ordem), o gate de risco decide por ultimo e licoes so
entram no prompt apos aprovacao humana.
"""

import re
import unicodedata

_INVISIBLE = re.compile(r"[­͏؜᠎​-‏‪-‮⁠-⁤⁦-⁯﻿]")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
# confusaveis mais comuns (cirilico/grego -> latim); o NFKC ja cuida de largura total e similares
_CONFUSABLES = str.maketrans(
    "авсеіјкморѕтухАВСЕНІЈКМОРЅТХαβεικνορτυχΑΒΕΗΙΚΜΝΟΡΤΧΖ",
    "abceijkmopstyxABCEHIJKMOPSTXabeikvoptuxABEHIKMNOPTXZ",
)
_MARKUP = re.compile(
    r"</?[A-Za-z][^>\n]{0,40}>|<\|[^|>\n]{0,40}\|>|```|~~~|\[/?(?:INST|SYS)\]", re.IGNORECASE
)
_ROLE = re.compile(r"(?im)\b(?:system|assistant|developer|user|human)\s*:")
_INSTRUCTION = re.compile(
    r"(?i)\b(?:ignore|ignora|desconsidere|esque[cç]a|disregard|override|forget)\b[^.\n]{0,60}"
    r"\b(?:rules?|regras?|instru[cç][oõ]es|instructions?|prompts?|previous|anteriores?)\b"
    r"|\b(?:aprove|approve|aumente|increase|raise|eleve)\b[^.\n]{0,40}"
    r"\b(?:risk|risco|size|tamanho|stop|leverage)\b"
)
_ABOVE = re.compile(
    r"(?i)\b(?:ignore|ignora|disregard|forget)\b[^.\n]{0,30}\b(?:above|acima|everything|tudo)\b"
)
_SECRET = re.compile(
    r"(?i)\bsk-[a-z0-9_-]{8,}|\bbearer\s+[a-z0-9._~+/=-]{8,}"
    r"|\b(?:api[_-]?key|token|secret|password)\s*[=:]\s*\S+"
)
_SPACES = re.compile(r"\s+")
REMOVED = "[removido]"


def sanitize_text(value: object, max_chars: int) -> str:
    """Normaliza (NFKC, confusaveis, invisiveis), tira controles/marcacao/papeis/frases de comando/segredos,
    colapsa espacos e trunca."""
    if not isinstance(value, str):
        raise TypeError("texto esperado")
    text = _INVISIBLE.sub("", unicodedata.normalize("NFKC", value))
    text = _INVISIBLE.sub("", text.translate(_CONFUSABLES))
    text = _CONTROL.sub(" ", text)
    for pattern in (_MARKUP, _ROLE, _INSTRUCTION, _ABOVE, _SECRET):
        text = pattern.sub(REMOVED, text)
    text = _SPACES.sub(" ", text).strip()
    return text[:max_chars].rstrip()
