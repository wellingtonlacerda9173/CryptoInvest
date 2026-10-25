import ipaddress
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import structlog
from pydantic import Field, SecretStr, ValidationInfo, field_validator, model_validator
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict
from sqlalchemy import URL

from engine.infrastructure.config.settings import MissingSecretError, resolve_secrets_dir
from engine.runner.command_channel import load_command_key

log = structlog.get_logger()

_SECRET_FIELDS = frozenset(
    {
        "session_secret",
        "master_keys",
        "command_hmac_key",
        "recovery_hmac_key",
        "live_authorization_hmac_key",
        "database_password",
    }
)
MAX_LIVE_INITIAL_CAPITAL_CAP = Decimal(100)
ENV_PREFIX = "API_"


class _WithoutSecrets(PydanticBaseSettingsSource):
    """Descarta segredos vindos de ambiente/.env: so arquivos em SECRETS_DIR valem."""

    def __init__(self, settings_cls: type[BaseSettings], inner: PydanticBaseSettingsSource) -> None:
        super().__init__(settings_cls)
        self._inner = inner

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return self._inner.get_field_value(field, field_name)

    def __call__(self) -> dict[str, Any]:
        values = self._inner()
        for name in sorted(_SECRET_FIELDS & values.keys()):
            log.warning("secret_from_environment_ignored", field=name)
        return {k: v for k, v in values.items() if k not in _SECRET_FIELDS}


class ApiSettings(BaseSettings):
    """Configuracao da API (prefixo `API_`). Segredos: arquivos `api_session_secret` e `api_master_keys`."""

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX, env_file=".env", extra="ignore", frozen=True, secrets_dir=None
    )

    environment: Literal["dev", "test", "prod"] = "dev"
    log_level: str = "INFO"

    panel_origins: list[str] = ["http://localhost:3000"]
    cookie_secure: bool = True
    # So ligue atras de proxy reverso. Exposta direto, o cliente forja o X-Forwarded-For e foge do throttle.
    # Com a opcao ligada, so o IP de `trusted_proxies` (IPs ou CIDRs) e acreditado como intermediario: o IP do
    # cliente e o primeiro salto NAO confiavel contado da direita; em producao a lista e obrigatoria.
    trust_forwarded_for: bool = False
    trusted_proxies: list[str] = []
    hsts_max_age_seconds: int = Field(default=0, ge=0)
    max_body_bytes: int = Field(default=64 * 1024, ge=1024, le=10 * 1024 * 1024)
    skip_schema_check: bool = False

    session_idle_minutes: int = Field(default=30, ge=1)
    session_absolute_hours: int = Field(default=12, ge=1)
    pending_login_minutes: int = Field(default=5, ge=1)
    fresh_auth_minutes: int = Field(default=5, ge=1)

    totp_issuer: str = "CryptoInvest"
    argon2_time_cost: int = Field(default=3, ge=1)
    argon2_memory_kib: int = Field(default=64 * 1024, ge=8)
    argon2_parallelism: int = Field(default=2, ge=1)

    # Defesa do login: hashes Argon2 concorrentes (memoria/CPU), espera maxima na fila e atraso por usuario
    login_max_concurrent_hashes: int = Field(default=4, ge=1, le=64)
    login_hash_queue_timeout_seconds: float = Field(default=2.0, gt=0)
    login_user_delay_base_seconds: float = Field(default=0.25, ge=0)
    login_user_delay_cap_seconds: float = Field(default=2.0, ge=0)
    login_alert_failures: int = Field(default=10, ge=3)
    activation_token_hours: int = Field(default=24, ge=1, le=168)
    # Segundos entre consultas ao banco por usuario BLOQUEADO para saber se outro processo (CLI, outro worker)
    # o desbloqueou (`users.lock_epoch`); 0 consulta a cada tentativa bloqueada (testes)
    unlock_recheck_seconds: float = Field(default=5.0, ge=0)
    # Codigos de recuperacao antigos (HMAC com session_secret) continuam validos ate todos regenerarem.
    # Janela de migracao: em producao desligue assim que todos regenerarem (aviso na inicializacao).
    recovery_accept_legacy_hashes: bool = True

    # Papel do banco da API (cryptoinvest_api): URL propria, separada da do robo (ver scripts/db_roles.sql).
    # Sem estes campos a API usa as credenciais DATABASE_* do robo (so aceitavel em dev).
    database_user: str | None = None
    database_password: SecretStr | None = None

    binance_api_origin: str = "https://api.binance.com"

    # Trava do Live (`POST /live/activate`): so grava uma AUTORIZACAO; nada liga o Live nesta versao
    live_initial_capital_cap_quote: Decimal = Field(default=MAX_LIVE_INITIAL_CAPITAL_CAP, gt=0)
    live_authorization_hours: int = Field(default=24, ge=1, le=168)
    live_key_max_age_hours: int = Field(default=24, ge=1, le=168)
    live_require_ip_restriction: bool = True
    live_block_on_key_warnings: bool = True
    binance_verify_timeout_seconds: float = Field(default=10.0, gt=0)

    # Canal Redis do runner (comandos assinados, estado e eventos em tempo real)
    # padrao: o mesmo REDIS_URL do runner (ver `create_default_app`); sobrescreva com API_REDIS_URL
    redis_url: str | None = None
    redis_prefix: str = "cryptoinvest:paper"
    runner_heartbeat_stale_seconds: int = Field(default=90, ge=10)
    command_pump_seconds: float = Field(default=2.0, gt=0)
    # Diretorio de dados do runner (`data/paper`): fallback por arquivo e leitura de `status.json`
    runner_data_dir: Path | None = None
    # Calendario de eventos mantido pelo atualizador (somente leitura aqui); sem ele o estado e `expired`
    calendar_path: Path | None = None
    # Comando que inicia o runner (ex.: ["uv","run","python","-m","engine.runner.paper","start"]); sem
    # ele a API nao liga o robo (so o servico externo liga) e responde com instrucoes
    runner_start_command: list[str] | None = None

    ws_max_connections_per_user: int = Field(default=5, ge=1)
    ws_queue_size: int = Field(default=200, ge=10)
    ws_max_dropped_events: int = Field(default=100, ge=1)
    ws_send_timeout_seconds: float = Field(default=5.0, gt=0)
    ws_ping_seconds: float = Field(default=20.0, gt=0)
    ws_session_check_seconds: float = Field(default=30.0, gt=0)
    ws_replay_buffer: int = Field(default=1000, ge=10)

    backtest_max_concurrent: int = Field(default=1, ge=1, le=4)
    backtest_timeout_seconds: int = Field(default=1800, ge=1)
    backtest_max_memory_mb: int = Field(default=2048, ge=64)
    backtest_max_years: float = Field(default=5.0, gt=0, le=10)
    backtest_cache_dir: Path = Path("data/cache")
    backtest_use_db: bool = True

    session_secret: SecretStr
    master_keys: SecretStr
    command_hmac_key: SecretStr | None = None
    # Segredo proprio do HMAC dos codigos de recuperacao (arquivo `api_recovery_hmac_key`); sem ele cai no
    # session_secret (legado). Trocar o valor invalida os codigos gerados com o anterior.
    recovery_hmac_key: SecretStr | None = None
    # HMAC do `record_hash` das autorizacoes do Live (arquivo `api_live_authorization_hmac_key`); sem ele cai
    # na `command_hmac_key`. Sem nenhuma das duas a autorizacao NAO e gravada.
    live_authorization_hmac_key: SecretStr | None = None

    @field_validator("panel_origins")
    @classmethod
    def _strict_origins(cls, origins: list[str]) -> list[str]:
        for origin in origins:
            parts = urlsplit(origin)
            valid = (
                origin != "*"
                and parts.scheme in {"http", "https"}
                and bool(parts.hostname)
                and parts.path in {"", "/"}
                and not (parts.query or parts.fragment or parts.username)
            )
            if not valid:
                raise ValueError(
                    f"origem de CORS invalida (use esquema://host[:porta], sem curinga): {origin!r}"
                )
        return [o.rstrip("/") for o in origins]

    @field_validator("trusted_proxies")
    @classmethod
    def _valid_proxies(cls, proxies: list[str]) -> list[str]:
        for item in proxies:
            try:
                ipaddress.ip_network(item, strict=False)
            except ValueError:
                raise ValueError(f"proxy confiavel invalido (use IP ou CIDR): {item!r}") from None
        return proxies

    @field_validator("command_hmac_key", "recovery_hmac_key", "live_authorization_hmac_key")
    @classmethod
    def _long_hmac_key(cls, value: SecretStr | None, info: ValidationInfo) -> SecretStr | None:
        if value is None:
            return None
        text = value.get_secret_value().strip()
        if len(text.encode()) < 32:
            raise ValueError(f"{info.field_name} precisa de ao menos 32 bytes")
        return SecretStr(text)

    @field_validator("session_secret")
    @classmethod
    def _long_secret(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value().strip()) < 32:
            raise ValueError("session_secret precisa de ao menos 32 caracteres")
        return SecretStr(value.get_secret_value().strip())

    @model_validator(mode="after")
    def _production_guards(self) -> "ApiSettings":
        if self.environment == "prod":
            if not self.live_require_ip_restriction or not self.live_block_on_key_warnings:
                raise ValueError(
                    "producao nao permite relaxar a trava do Live (live_require_ip_restriction e "
                    "live_block_on_key_warnings precisam ser true)"
                )
            if self.live_initial_capital_cap_quote > MAX_LIVE_INITIAL_CAPITAL_CAP:
                raise ValueError(
                    "producao nao permite teto de capital inicial do Live acima de "
                    f"{MAX_LIVE_INITIAL_CAPITAL_CAP}"
                )
            if self.skip_schema_check:
                raise ValueError("producao nao permite skip_schema_check (--skip-schema-check e so para dev)")
            if not self.cookie_secure:
                raise ValueError("producao exige cookie_secure=true")
            if any(not o.startswith("https://") for o in self.panel_origins):
                raise ValueError("producao exige origens do painel em https")
            if self.hsts_max_age_seconds == 0:
                raise ValueError("producao exige HSTS (hsts_max_age_seconds > 0)")
            if urlsplit(self.binance_api_origin).scheme != "https":
                raise ValueError(
                    "producao exige binance_api_origin em https (chave e assinatura trafegam nele)"
                )
            if self.trust_forwarded_for and not self.trusted_proxies:
                raise ValueError(
                    "producao com trust_forwarded_for exige trusted_proxies (IPs/CIDRs do proxy)"
                )
            if not self.database_user or self.database_password is None:
                raise ValueError(
                    "producao exige o papel de banco dedicado da API: defina database_user "
                    "(API_DATABASE_USER=cryptoinvest_api) e o secret api_database_password"
                )
            if not self.trust_forwarded_for:
                log.warning(
                    "prod_without_trust_forwarded_for",
                    hint="atras de proxy reverso defina API_TRUST_FORWARDED_FOR=true e API_TRUSTED_PROXIES; "
                    "senao todos os clientes compartilham o IP do proxy no throttle e na auditoria",
                )
            if self.recovery_accept_legacy_hashes and self.recovery_hmac_key is not None:
                log.warning(
                    "recovery_legacy_hashes_enabled_in_production",
                    hint="desligue API_RECOVERY_ACCEPT_LEGACY_HASHES quando todos regenerarem os codigos",
                )
        return self

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
    def proxy_networks(self) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
        return [ipaddress.ip_network(item, strict=False) for item in self.trusted_proxies]

    @property
    def session_cookie_name(self) -> str:
        return "__Host-ci_session" if self.cookie_secure else "ci_session"

    @property
    def csrf_cookie_name(self) -> str:
        return "__Host-ci_csrf" if self.cookie_secure else "ci_csrf"


def api_database_url(settings: ApiSettings, base: URL) -> URL:
    """URL do banco da API: credenciais do papel `cryptoinvest_api` se configuradas, senao as do robo."""
    if settings.database_user is None and settings.database_password is None:
        return base
    url = base.set(
        username=settings.database_user or base.username,
        password=settings.database_password.get_secret_value()
        if settings.database_password
        else base.password,
    )
    if settings.environment == "prod" and url.username == base.username:
        raise ValueError("producao exige papel de banco da API distinto do papel do robo (DATABASE_USER)")
    return url


def load_api_settings() -> ApiSettings:
    secrets_dir = resolve_secrets_dir()
    try:
        settings = ApiSettings(_secrets_dir=secrets_dir)
        if settings.command_hmac_key is None and (shared := load_command_key(secrets_dir)) is not None:
            # O arquivo `command_hmac_key` e COMPARTILHADO com o runner (sem o prefixo api_ do pydantic).
            settings = settings.model_copy(update={"command_hmac_key": SecretStr(shared.decode())})
        return settings
    except Exception as exc:
        missing = [
            name
            for error in getattr(exc, "errors", lambda: [])()
            if (name := str(error["loc"][0])) in _SECRET_FIELDS and error["type"] == "missing"
        ]
        if missing:
            raise MissingSecretError(
                f"segredos da API ausentes ({', '.join(f'{ENV_PREFIX.lower()}{m}' for m in missing)}): "
                "rode `python -m api.cli init-secrets` ou crie os arquivos em SECRETS_DIR."
            ) from exc
        raise
