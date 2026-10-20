import os
from pathlib import Path
from typing import Any

import structlog
from dotenv import dotenv_values
from pydantic import SecretStr, ValidationError
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict
from sqlalchemy import URL

log = structlog.get_logger()

DEFAULT_SECRETS_DIR = "/run/secrets"
_SECRET_FIELDS = frozenset({"database_password", "binance_api_key", "binance_api_secret"})


class _WithoutSecrets(PydanticBaseSettingsSource):
    def __init__(self, settings_cls: type[BaseSettings], inner: PydanticBaseSettingsSource) -> None:
        super().__init__(settings_cls)
        self._inner = inner

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return self._inner.get_field_value(field, field_name)

    def __call__(self) -> dict[str, Any]:
        values = self._inner()
        for name in sorted(_SECRET_FIELDS & values.keys()):
            log.warning(
                "secret_from_environment_ignored",
                field=name,
                source=type(self._inner).__name__,
                hint="segredos so sao lidos de arquivos em SECRETS_DIR",
            )
        return {k: v for k, v in values.items() if k not in _SECRET_FIELDS}


class MissingSecretError(RuntimeError):
    """Segredo obrigatorio ausente."""


class Settings(BaseSettings):
    """Configuracao. Segredos vem exclusivamente de arquivos (padrao Docker secrets)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", frozen=True)

    environment: str = "dev"
    log_level: str = "INFO"

    database_host: str = "localhost"
    database_port: int = 5432
    database_name: str = "cryptoinvest"
    database_user: str = "cryptoinvest"
    database_password: SecretStr

    redis_url: str = "redis://localhost:6379/0"

    binance_rest_url: str = "https://api.binance.com/api/v3"
    binance_ws_url: str = "wss://stream.binance.com:9443"
    # o runner do paper usa a porta 443 (a 9443 e bloqueada em algumas redes); sobrescreva por PAPER_WS_URL
    paper_ws_url: str = "wss://stream.binance.com:443"
    binance_api_key: SecretStr | None = None
    binance_api_secret: SecretStr | None = None

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            _WithoutSecrets(settings_cls, env_settings),
            _WithoutSecrets(settings_cls, dotenv_settings),
            file_secret_settings,
        )

    @property
    def database_url(self) -> URL:
        return URL.create(
            "postgresql+asyncpg",
            username=self.database_user,
            password=self.database_password.get_secret_value(),
            host=self.database_host,
            port=self.database_port,
            database=self.database_name,
        )


def resolve_secrets_dir() -> Path | None:
    configured = os.environ.get("SECRETS_DIR") or dotenv_values(".env").get("SECRETS_DIR")
    path = Path(configured or DEFAULT_SECRETS_DIR)
    return path if path.is_dir() else None


def load_settings() -> Settings:
    secrets_dir = resolve_secrets_dir()
    try:
        return Settings(_secrets_dir=secrets_dir)
    except ValidationError as exc:
        if any(
            error["loc"] == ("database_password",) and error["type"] == "missing" for error in exc.errors()
        ):
            where = secrets_dir or f"{DEFAULT_SECRETS_DIR} (inexistente; defina SECRETS_DIR)"
            raise MissingSecretError(
                f"database_password ausente: crie o arquivo 'database_password' em {where}. "
                "Variaveis de ambiente e .env nao sao usadas para segredos."
            ) from exc
        raise
