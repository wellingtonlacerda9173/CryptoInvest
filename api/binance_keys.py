import hashlib
import hmac
import time
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlencode

import aiohttp
import structlog
from pydantic import BaseModel

from engine.infrastructure.config.logging import scrub_text

log = structlog.get_logger()


class KeyVerificationError(Exception):
    """Falha ao verificar a chave. A mensagem e sempre segura para exibir (sem segredo)."""

    def __init__(self, message: str, *, upstream: bool = False) -> None:
        super().__init__(message)
        self.upstream = upstream


class KeyPermissions(BaseModel):
    """Permissoes detectadas na corretora. Guardadas em claro: nao revelam a chave."""

    can_read: bool
    can_trade: bool
    can_withdraw: bool
    can_transfer: bool = False
    margin_enabled: bool = False
    futures_enabled: bool = False
    ip_restricted: bool | None = None


class ExchangeKeyVerifier(Protocol):
    async def verify(self, api_key: str, api_secret: str) -> KeyPermissions: ...


@dataclass(slots=True)
class FakeKeyVerifier:
    """Verificador de testes: devolve permissoes pre-definidas e registra as chamadas (sem segredo)."""

    permissions: KeyPermissions = field(
        default_factory=lambda: KeyPermissions(
            can_read=True, can_trade=True, can_withdraw=False, ip_restricted=True
        )
    )
    error: str | None = None
    calls: int = 0

    async def verify(self, api_key: str, api_secret: str) -> KeyPermissions:
        self.calls += 1
        if self.error:
            raise KeyVerificationError(self.error)
        return self.permissions


class BinanceKeyVerifier:
    """Consulta `GET /sapi/v1/account/apiRestrictions` (somente leitura; nenhuma ordem e enviada)."""

    def __init__(self, origin: str, *, timeout_seconds: float = 10.0) -> None:
        self._origin = origin.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)

    async def verify(self, api_key: str, api_secret: str) -> KeyPermissions:
        query = urlencode({"timestamp": int(time.time() * 1000), "recvWindow": 5000})
        signature = hmac.new(api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        url = f"{self._origin}/sapi/v1/account/apiRestrictions?{query}&signature={signature}"
        try:
            async with (
                aiohttp.ClientSession(timeout=self._timeout) as http,
                http.get(url, headers={"X-MBX-APIKEY": api_key}) as response,
            ):
                payload: Any = await response.json(content_type=None)
                status = response.status
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            log.warning("binance_key_verification_unreachable", error=scrub_text(type(exc).__name__))
            raise KeyVerificationError(
                "Nao foi possivel falar com a Binance para validar a chave. Tente de novo em instantes.",
                upstream=True,
            ) from None
        if status != 200:
            code = payload.get("code") if isinstance(payload, dict) else None
            raise KeyVerificationError(
                "A Binance recusou a chave. Confira se a chave e o segredo estao corretos"
                + (f" (codigo {code})." if isinstance(code, int) else ".")
            )
        return _permissions_from(payload)


_REQUIRED_FLAGS = ("enableReading", "enableSpotAndMarginTrading", "enableWithdrawals")


def _permissions_from(payload: Any) -> KeyPermissions:
    """Falha FECHADA: sem as tres flags booleanas explicitas nao ha como afirmar que a chave nao saca."""
    if not isinstance(payload, dict) or any(
        not isinstance(payload.get(flag), bool) for flag in _REQUIRED_FLAGS
    ):
        log.warning("binance_key_verification_malformed_payload")
        raise KeyVerificationError(
            "A resposta da Binance nao permitiu confirmar as permissoes da chave. "
            "Nao foi possivel validar; tente de novo ou use outra chave."
        )
    return KeyPermissions(
        can_read=payload["enableReading"],
        can_trade=payload["enableSpotAndMarginTrading"],
        can_withdraw=payload["enableWithdrawals"],
        can_transfer=payload.get("enableInternalTransfer") is True
        or payload.get("permitsUniversalTransfer") is True,
        margin_enabled=payload.get("enableMargin") is True,
        futures_enabled=payload.get("enableFutures") is True,
        ip_restricted=ip if isinstance(ip := payload.get("ipRestrict"), bool) else None,
    )
