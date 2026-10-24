import hmac
from datetime import UTC, datetime

import pyotp

STEP_SECONDS = 30
WINDOW = 1
DIGITS = 6


def new_secret() -> str:
    return pyotp.random_base32()


def provisioning_uri(secret: str, *, username: str, issuer: str) -> str:
    return pyotp.TOTP(secret, digits=DIGITS, interval=STEP_SECONDS).provisioning_uri(
        name=username, issuer_name=issuer
    )


def counter_at(moment: datetime) -> int:
    return int(moment.astimezone(UTC).timestamp()) // STEP_SECONDS


def current_code(secret: str, moment: datetime) -> str:
    return pyotp.TOTP(secret, digits=DIGITS, interval=STEP_SECONDS).at(moment)


def match_counter(secret: str, code: str, moment: datetime, *, last_counter: int | None) -> int | None:
    """Contador do passo cujo codigo confere (janela +-1), ou None.

    Anti-replay: so aceita contador estritamente maior que o ultimo ja consumido. O chamador
    grava o contador de forma atomica (UPDATE condicional) para fechar a corrida entre requisicoes.
    """
    candidate = code.strip().replace(" ", "")
    if len(candidate) != DIGITS or not candidate.isascii() or not candidate.isdigit():
        return None
    totp = pyotp.TOTP(secret, digits=DIGITS, interval=STEP_SECONDS)
    center = counter_at(moment)
    matched: int | None = None
    for counter in range(center - WINDOW, center + WINDOW + 1):
        expected = totp.at(counter * STEP_SECONDS)
        if hmac.compare_digest(expected, candidate) and (last_counter is None or counter > last_counter):
            matched = counter
    return matched
