"""Configuracao versionada do robo: pares, estrategias, risco e IA, num documento unico e imutavel.

O documento e a fonte da verdade lida pelo runner no start. Os tetos rigidos de risco continuam no dominio
(`RiskConfig`): nada aqui os relaxa, e o servidor rejeita valores acima deles.
"""

import hashlib
import json
from decimal import Decimal
from typing import Annotated, Any, Literal, Self

from pydantic import Field, StringConstraints, model_validator

from engine.application.detectors import ElephantBarConfig
from engine.domain.base import DomainModel
from engine.domain.risk import RiskConfig
from engine.domain.rules import DEFAULT_MIN_REWARD_RISK
from engine.domain.types import StrictDecimal, Symbol, Timeframe

Section = Literal["pairs", "strategies", "risk", "ai"]
SECTIONS: tuple[Section, ...] = ("pairs", "strategies", "risk", "ai")
MAX_PAIRS = 10
DEFAULT_AI_MODEL = "claude-haiku-5-5"
# formato do digest antes da Fase 6: o hash dos documentos (e dos runs) existentes nao pode mudar
LEGACY_AI_DIGEST: dict[str, Any] = {"enabled": False, "model": "claude-sonnet-5-5"}
MAX_AI_MONTHLY_BUDGET_USD = Decimal(200)
MAX_AI_TIMEOUT_SECONDS = 15.0
ModelName = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[A-Za-z0-9._:-]{1,64}$")]


class PairsConfig(DomainModel):
    symbols: tuple[Symbol, ...] = Field(default=("BTC/USDT", "ETH/USDT"), min_length=1, max_length=MAX_PAIRS)
    timeframe: Timeframe = Timeframe.H1

    @model_validator(mode="after")
    def _unique(self) -> Self:
        if len(set(self.symbols)) != len(self.symbols):
            raise ValueError("pares repetidos")
        return self


class EmaParams(DomainModel):
    fast_period: int = Field(default=9, ge=2, le=200)
    slow_period: int = Field(default=21, ge=3, le=400)
    trend_period: int = Field(default=50, ge=4, le=600)
    adx_min: float = Field(default=20.0, gt=0, le=100)
    rsi_min: float = Field(default=50.0, ge=0, lt=100)
    rsi_max: float = Field(default=70.0, gt=0, le=100)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if not self.fast_period < self.slow_period:
            raise ValueError("a media rapida deve ser menor que a lenta")
        if not self.rsi_min < self.rsi_max:
            raise ValueError("o RSI minimo deve ser menor que o maximo")
        return self


class ElephantParams(DomainModel):
    detector: ElephantBarConfig = ElephantBarConfig()
    rsi_min: float = Field(default=50.0, ge=0, lt=100)
    rsi_max: float = Field(default=75.0, gt=0, le=100)
    strong_volume_multiple: float = Field(default=2.0, gt=0, le=20)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if not self.strong_volume_multiple > self.detector.volume_multiple:
            raise ValueError("o volume forte deve superar o multiplo de volume do detector")
        if not self.rsi_min < self.rsi_max:
            raise ValueError("o RSI minimo deve ser menor que o maximo")
        return self


class StrategiesConfig(DomainModel):
    elephant_enabled: bool = True
    ema_enabled: bool = True
    min_confidence: float = Field(default=0.5, ge=0, le=1)
    reward_risk: StrictDecimal = Field(default=DEFAULT_MIN_REWARD_RISK, ge=DEFAULT_MIN_REWARD_RISK, le=10)
    elephant: ElephantParams = ElephantParams()
    ema: EmaParams = EmaParams()

    @model_validator(mode="after")
    def _one_enabled(self) -> Self:
        if not (self.elephant_enabled or self.ema_enabled):
            raise ValueError("ligue ao menos uma estrategia")
        return self

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(n for n, on in (("elephant", self.elephant_enabled), ("ema", self.ema_enabled)) if on)


class AiConfig(DomainModel):
    """A IA so pode vetar ou reduzir a confianca de um sinal ja aprovado pelo gate: nenhum campo aqui chega
    ao motor de ordens ou de risco. Orcamento e prazo tem tetos rigidos; o padrao do modelo e o mais barato
    da familia atual (decisao de custo, nao de risco; troque em `ai.model`)."""

    enabled: bool = False
    model: ModelName = DEFAULT_AI_MODEL
    monthly_budget_usd: StrictDecimal = Field(default=Decimal(20), ge=0, le=MAX_AI_MONTHLY_BUDGET_USD)
    timeout_seconds: float = Field(default=8.0, gt=0, le=MAX_AI_TIMEOUT_SECONDS)


class ConfigDocument(DomainModel):
    pairs: PairsConfig = PairsConfig()
    strategies: StrategiesConfig = StrategiesConfig()
    risk: RiskConfig = RiskConfig()
    ai: AiConfig = AiConfig()

    def dump(self) -> dict[str, Any]:
        """JSON canonico: conjuntos viram listas ordenadas (o hash precisa ser deterministico)."""
        data = self.model_dump(mode="json")
        for name in ("allowed_assets", "blocked_assets"):
            data["risk"][name] = sorted(data["risk"][name])
        return data

    def digest(self) -> str:
        """Hash da versao inteira (cadeia `config_versions`). `ai` no padrao ou so com `enabled`/`model`
        mantem o formato legado, entao o hash de documentos anteriores a Fase 6 e identico."""
        data = self.dump()
        ai, default = self.ai, AiConfig()
        if ai == default:
            data["ai"] = dict(LEGACY_AI_DIGEST)
        elif (ai.monthly_budget_usd, ai.timeout_seconds) == (
            default.monthly_budget_usd,
            default.timeout_seconds,
        ):
            data["ai"] = {"enabled": ai.enabled, "model": ai.model}
        return canonical_hash(data)

    def identity_digest(self) -> str:
        """Identidade do RUN (`config_hash`): a IA e uma camada que so reduz e acompanha a versao nova a cada
        start; ligar/desligar, trocar modelo, orcamento ou prazo nao pode impedir a retomada de um run com
        posicao aberta. Fica sempre no formato legado, igual ao hash dos runs anteriores."""
        data = self.dump()
        data["ai"] = dict(LEGACY_AI_DIGEST)
        return canonical_hash(data)

    def with_section(self, section: Section, value: dict[str, Any]) -> "ConfigDocument":
        data = self.dump()
        data[section] = value
        return ConfigDocument.model_validate(data)


def canonical_hash(payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def default_document() -> ConfigDocument:
    return ConfigDocument()
