from decimal import Decimal
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, ValidationInfo, field_validator, model_validator

from engine.domain.base import DomainModel
from engine.domain.types import StrictDecimal, Symbol

Environment = Literal["mainnet", "testnet", "local_fake"]

MAINNET_URL = "https://api.binance.com"
TESTNET_URL = "https://testnet.binance.vision"

# Hosts documentados pela Binance para a API Spot assinada. `data-api.binance.vision` e so de dados publicos.
MAINNET_HOSTS = frozenset(
    {
        "api.binance.com",
        "api-gcp.binance.com",
        "api1.binance.com",
        "api2.binance.com",
        "api3.binance.com",
        "api4.binance.com",
    }
)
TESTNET_HOSTS = frozenset({"testnet.binance.vision"})
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _check_remote(url: str, hosts: frozenset[str], label: str) -> None:
    parts = urlsplit(url)
    host = parts.hostname or ""
    clean = (
        parts.scheme == "https"
        and host in hosts
        and parts.netloc == host
        and parts.path in ("", "/")
        and not (parts.query or parts.fragment or parts.username or parts.password)
    )
    if not clean:
        raise ValueError(f"base_url fora da allowlist de {label}: {sorted(hosts)} (https, sem porta/caminho)")


def _check_local(url: str) -> None:
    parts = urlsplit(url)
    host = parts.hostname or ""
    netloc_host = f"[{host}]" if ":" in host else host
    clean = (
        parts.scheme == "http"
        and host in LOCAL_HOSTS
        and parts.netloc in (netloc_host, f"{netloc_host}:{parts.port}")
        and parts.path in ("", "/")
        and not (parts.query or parts.fragment or parts.username or parts.password)
    )
    if not clean:
        raise ValueError(f"base_url local deve ser http://{sorted(LOCAL_HOSTS)} exato (sem usuario/caminho)")


class LiveConfig(DomainModel):
    """Parametros do gateway Live. Tudo conservador por padrao; `read_only` liga por omissao.

    `environment` e obrigatorio e conferido contra o host: mainnet so aceita hosts oficiais da Binance;
    testnet exige `testnet=True`; a fake local exige `allow_local_fake=True` (flag de testes: nunca derive
    de variavel de ambiente ou configuracao de producao).
    """

    environment: Environment
    testnet: bool = False
    allow_local_fake: bool = False
    base_url: str = MAINNET_URL
    allowed_symbols: frozenset[Symbol] = frozenset()
    read_only: bool = True
    capital_cap_quote: StrictDecimal = Field(gt=0)
    quote_asset: str = "USDT"
    recv_window_ms: int = Field(default=5000, ge=1000, le=60000)
    max_clock_drift_ms: int = Field(default=1000, ge=50, le=10000)
    time_resync_seconds: float = Field(default=300.0, gt=0)
    request_timeout_seconds: float = Field(default=10.0, gt=0)
    read_retries: int = Field(default=3, ge=0, le=10)
    order_lookup_retries: int = Field(default=3, ge=1, le=10)
    protection_retries: int = Field(default=3, ge=1, le=10)
    emergency_max_attempts: int = Field(default=6, ge=1, le=20)
    protection_deadline_seconds: float = Field(default=20.0, gt=0, le=120)
    poll_blind_threshold: int = Field(default=5, ge=1, le=100)
    weight_limit_per_minute: int = Field(default=6000, ge=100)
    weight_safety_fraction: StrictDecimal = Field(default=Decimal("0.8"), gt=0, le=1)
    filters_ttl_seconds: float = Field(default=600.0, gt=0)
    partial_fill_grace_seconds: float = Field(default=5.0, ge=0)
    protection_activation_grace_seconds: float = Field(default=3.0, ge=0)
    stop_leg: Literal["stop_loss", "stop_loss_limit"] = "stop_loss"
    stop_limit_offset_bps: StrictDecimal = Field(default=Decimal(30), ge=1, le=500)
    min_stop_distance_bps: StrictDecimal = Field(default=Decimal(10), ge=1)
    poll_interval_seconds: float = Field(default=1.0, gt=0)
    fallback_fee_rate: StrictDecimal = Field(default=Decimal("0.001"), ge=0, le=Decimal("0.01"))

    @field_validator("base_url")
    @classmethod
    def _check_url(cls, value: str, info: ValidationInfo) -> str:
        # urlsplit descarta quebra de linha/tab em silencio: o valor cru iria ao cliente HTTP.
        if not value.isascii() or any(ch.isspace() or not ch.isprintable() for ch in value):
            raise ValueError("base_url nao pode conter espaco, caractere de controle ou nao-ASCII")
        environment = info.data.get("environment")
        match environment:
            case "mainnet":
                _check_remote(value, MAINNET_HOSTS, "mainnet")
            case "testnet":
                _check_remote(value, TESTNET_HOSTS, "testnet")
            case "local_fake":
                _check_local(value)
            case _:
                raise ValueError("base_url nao validavel sem environment valido")
        return value

    @model_validator(mode="after")
    def _check_flags(self) -> Self:
        if self.environment == "testnet" and not self.testnet:
            raise ValueError("environment=testnet exige testnet=True explicito")
        if self.environment != "testnet" and self.testnet:
            raise ValueError("testnet=True so e valido com environment=testnet")
        if self.environment == "local_fake" and not self.allow_local_fake:
            raise ValueError("environment=local_fake exige allow_local_fake=True (somente testes)")
        if self.environment != "local_fake" and self.allow_local_fake:
            raise ValueError("allow_local_fake so e valido com environment=local_fake")
        return self
