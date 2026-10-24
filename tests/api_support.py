"""Auxiliares dos testes da API: configuracao barata (Argon2 minimo), relogio controlavel e sessao TOTP."""

import base64
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import SecretStr

from api.config import ApiSettings

MASTER_KEY_V1 = base64.b64encode(bytes(range(32))).decode()
MASTER_KEY_V2 = base64.b64encode(bytes(range(32, 64))).decode()
GOOD_PASSWORD = "cavalo azul voa sobre 7 montanhas!"
OTHER_PASSWORD = "bule roxo canta tres musicas 42?"


def make_settings(**overrides: Any) -> ApiSettings:
    values: dict[str, Any] = {
        "environment": "test",
        "cookie_secure": False,
        "panel_origins": ["http://localhost:3000"],
        "argon2_time_cost": 1,
        "argon2_memory_kib": 8,
        "argon2_parallelism": 1,
        "login_user_delay_base_seconds": 0.01,
        "login_user_delay_cap_seconds": 0.05,
        "unlock_recheck_seconds": 0.0,
        "session_secret": SecretStr(secrets.token_urlsafe(48)),
        "master_keys": SecretStr(f"1:{MASTER_KEY_V1}"),
        # prefixo proprio: os testes nunca leem nem escrevem as chaves Redis de um robo de desenvolvimento
        "redis_prefix": f"cryptoinvest:test:{secrets.token_hex(4)}",
    }
    values.update(overrides)
    return ApiSettings(**values)


@dataclass(slots=True)
class FakeClock:
    now: datetime = field(default_factory=lambda: datetime.now(UTC).replace(microsecond=0))

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)
