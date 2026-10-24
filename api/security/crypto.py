import base64
import re
import secrets
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_BYTES = 32
NONCE_BYTES = 12
_ENVELOPE = re.compile(r"^enc:v(?P<version>[1-9][0-9]{0,5}):(?P<blob>[A-Za-z0-9_-]+)$")


class DecryptionError(Exception):
    """Envelope adulterado, AAD diferente ou chave desconhecida. Nunca carrega o conteudo."""


class KeyringError(ValueError):
    """Arquivo de chaves mestras invalido."""


@dataclass(frozen=True, slots=True)
class KeyRing:
    """Chaves mestras versionadas: a de maior versao cifra; as demais so decifram (rotacao)."""

    keys: dict[int, bytes]

    def __post_init__(self) -> None:
        if not self.keys:
            raise KeyringError("nenhuma chave mestra configurada")
        if any(len(key) != KEY_BYTES for key in self.keys.values()):
            raise KeyringError(f"cada chave mestra deve ter {KEY_BYTES} bytes")

    def __repr__(self) -> str:
        return f"KeyRing(versions={sorted(self.keys)})"

    @property
    def active_version(self) -> int:
        return max(self.keys)

    @classmethod
    def parse(cls, text: str) -> "KeyRing":
        """Linhas `versao:base64(32 bytes)`; linhas vazias e `#` sao ignoradas."""
        keys: dict[int, bytes] = {}
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            version_text, sep, encoded = line.partition(":")
            if not sep or not version_text.isdigit() or int(version_text) < 1:
                raise KeyringError("formato esperado por linha: <versao>:<chave base64>")
            version = int(version_text)
            if version in keys:
                raise KeyringError(f"versao de chave duplicada: {version}")
            try:
                keys[version] = base64.b64decode(encoded.strip(), validate=True)
            except ValueError as exc:
                raise KeyringError(f"chave da versao {version} nao e base64 valido") from exc
        return cls(keys)

    def encrypt(self, plaintext: str, *, aad: str) -> str:
        version = self.active_version
        nonce = secrets.token_bytes(NONCE_BYTES)
        sealed = AESGCM(self.keys[version]).encrypt(nonce, plaintext.encode(), aad.encode())
        blob = base64.urlsafe_b64encode(nonce + sealed).decode().rstrip("=")
        return f"enc:v{version}:{blob}"

    def decrypt(self, envelope: str, *, aad: str) -> str:
        match = _ENVELOPE.match(envelope)
        if match is None:
            raise DecryptionError("envelope invalido")
        key = self.keys.get(int(match["version"]))
        if key is None:
            raise DecryptionError("versao de chave desconhecida")
        try:
            raw = base64.urlsafe_b64decode(match["blob"] + "=" * (-len(match["blob"]) % 4))
            if len(raw) < NONCE_BYTES + 16:
                raise DecryptionError("envelope truncado")
            return AESGCM(key).decrypt(raw[:NONCE_BYTES], raw[NONCE_BYTES:], aad.encode()).decode()
        except (InvalidTag, ValueError):
            raise DecryptionError("falha de autenticacao") from None

    @staticmethod
    def version_of(envelope: str) -> int:
        match = _ENVELOPE.match(envelope)
        if match is None:
            raise DecryptionError("envelope invalido")
        return int(match["version"])

    def needs_rotation(self, envelope: str) -> bool:
        return self.version_of(envelope) != self.active_version


def generate_key_line(version: int) -> str:
    return f"{version}:{base64.b64encode(secrets.token_bytes(KEY_BYTES)).decode()}"
