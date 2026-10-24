"""Assinatura HMAC compartilhada entre runner e API para tudo que cruza o Redis.

O Redis e uma fronteira de confianca fraca: quem escreve nele pode forjar comandos, confirmacoes ou
estado. Todas as mensagens carregam um HMAC-SHA256 sobre o corpo canonico, com uma sub-chave derivada por
DOMINIO (`command`, `ack`, `event`) para que uma assinatura de um tipo nunca valha em outro. A chave-mae e a
mesma `command_hmac_key` (arquivo secreto). Os comandos mantem o formato original (chave-mae direta).
"""

import hashlib
import hmac
import json
from collections.abc import Mapping
from typing import Any, Final

DOMAIN_ACK: Final = "ack"
DOMAIN_EVENT: Final = "event"
MAX_JSON_DEPTH: Final = 8


def canonical(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def derive_key(key: bytes, domain: str) -> bytes:
    return hmac.new(key, f"cryptoinvest/v1/{domain}".encode(), hashlib.sha256).digest()


def sign_body(key: bytes, domain: str, subject: str, body: Mapping[str, Any]) -> str:
    """HMAC (hex) de `subject` + corpo canonico; `subject` amarra a assinatura ao id/tipo da mensagem."""
    message = subject.encode() + b"\n" + canonical(body)
    return hmac.new(derive_key(key, domain), message, hashlib.sha256).hexdigest()


def signatures_match(received: object, expected: str) -> bool:
    """Comparacao em tempo constante sobre bytes ASCII; o que nao for ASCII simplesmente nao confere."""
    if not isinstance(received, str) or not received.isascii():
        return False
    return hmac.compare_digest(received.encode("ascii"), expected.encode("ascii"))


def as_text(raw: object) -> str | None:
    """Texto UTF-8 estrito de um valor vindo do Redis (bytes ou str); bytes invalidos viram `None`."""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, bytes | bytearray | memoryview):
        try:
            return bytes(raw).decode("utf-8")
        except UnicodeDecodeError:
            return None
    return str(raw)


def too_deep(raw: str, limit: int = MAX_JSON_DEPTH) -> bool:
    """Aninhamento de {} e [] acima de `limit` (ignora o conteudo de strings); barato e sem recursao."""
    depth = 0
    in_string = escaped = False
    for char in raw:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "{[":
            depth += 1
            if depth > limit:
                return True
        elif char in "}]":
            depth -= 1
    return False


def wrap_event(key: bytes, kind: str, payload: Mapping[str, Any]) -> str:
    body = {"kind": kind, "payload": dict(payload)}
    return json.dumps(
        {**body, "sig": sign_body(key, DOMAIN_EVENT, kind, {"payload": body["payload"]})},
        separators=(",", ":"),
    )


def open_event(key: bytes, kind: str, raw: object) -> dict[str, Any] | None:
    """Payload do evento se o envelope e a assinatura conferem para o tipo esperado; senao `None`."""
    text = as_text(raw)
    if text is None or len(text) > 1_000_000 or too_deep(text, 32):
        return None
    try:
        envelope = json.loads(text)
        payload = envelope["payload"]
        if not isinstance(payload, dict) or envelope.get("kind") != kind:
            return None
        expected = sign_body(key, DOMAIN_EVENT, kind, {"payload": payload})
    except (ValueError, KeyError, TypeError, RecursionError):
        return None
    return payload if signatures_match(envelope.get("sig"), expected) else None


def sign_ack(key: bytes, command_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
    return {**body, "sig": sign_body(key, DOMAIN_ACK, command_id, body)}


def open_ack(key: bytes, command_id: str, raw: object) -> dict[str, Any] | None:
    text = as_text(raw)
    if text is None or len(text) > 4096 or too_deep(text, 4):
        return None
    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            return None
        body = {k: v for k, v in data.items() if k != "sig"}
        expected = sign_body(key, DOMAIN_ACK, command_id, body)
    except (ValueError, TypeError, RecursionError):
        return None
    return body if signatures_match(data.get("sig"), expected) else None
