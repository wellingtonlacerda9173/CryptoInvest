import base64
import hashlib
import hmac
import secrets
import uuid

RECOVERY_CODE_COUNT = 10
_RECOVERY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_RECOVERY_LENGTH = 16


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """Token de sessao tem 256 bits de entropia: SHA-256 sem salt basta e permite busca por indice."""
    return hashlib.sha256(token.encode()).hexdigest()


def csrf_token(secret: str, session_id: uuid.UUID) -> str:
    """Token CSRF derivado da sessao: plantar um cookie nao basta, o valor precisa casar com a sessao."""
    digest = hmac.new(secret.encode(), f"csrf:{session_id}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def new_recovery_codes(count: int = RECOVERY_CODE_COUNT) -> list[str]:
    codes: list[str] = []
    for _ in range(count):
        raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(_RECOVERY_LENGTH))
        codes.append("-".join(raw[i : i + 4] for i in range(0, _RECOVERY_LENGTH, 4)))
    return codes


def normalize_recovery_code(code: str) -> str:
    return code.strip().replace("-", "").replace(" ", "").upper()


def hash_recovery_code(secret: str, code: str) -> str:
    """HMAC com segredo do servidor: um vazamento so do banco nao permite forcar os codigos offline."""
    return hmac.new(
        secret.encode(), b"recovery:" + normalize_recovery_code(code).encode(), hashlib.sha256
    ).hexdigest()


def new_activation_token() -> str:
    """Token de ativacao do 2FA: uso unico, repassado ao usuario fora de banda; no banco so o hash."""
    return secrets.token_urlsafe(24)


def hash_activation_token(token: str) -> str:
    return hashlib.sha256(b"activation:" + token.encode()).hexdigest()


def username_tag(secret: str, username: str) -> str:
    """Rotulo curto e estavel (HMAC) de um nome digitado: correlaciona tentativas sem gravar texto livre."""
    digest = hmac.new(secret.encode(), b"username:" + username.encode("utf-8", "replace"), hashlib.sha256)
    return digest.hexdigest()[:16]
