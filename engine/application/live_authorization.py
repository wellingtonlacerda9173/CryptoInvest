"""Regras puras da trava de ativacao do Live. NADA aqui liga o Live: so decide se uma AUTORIZACAO vale.

Os pisos abaixo valem mesmo que `LiveGateThresholds` seja configurado de forma mais frouxa em outro lugar.
"""

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final

from engine.application.paper_metrics.metrics import PaperMetrics
from engine.domain.base import DomainModel

LIVE_PHRASE: Final = "OPERAR COM DINHEIRO REAL"
MIN_PAPER_DAYS: Final = 60
MAX_DRAWDOWN: Final = Decimal("0.05")
MAX_DAILY_LOSS: Final = Decimal("0.01")
MIN_PROFIT_FACTOR: Final = Decimal("1.5")
MIN_TRADES: Final = 30
MIN_LOSSES: Final = 5
REDUCED_CAPITAL_DAYS: Final = 7
LIMIT_INCREASE_DAYS: Final = 30


class Unmet(DomainModel):
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class KeyFacts:
    can_read: bool
    can_trade: bool
    can_withdraw: bool
    can_transfer: bool
    margin_enabled: bool
    futures_enabled: bool
    ip_restricted: bool | None
    verified_at: datetime | None


@dataclass(frozen=True, slots=True)
class ActivationPolicy:
    initial_capital_cap: Decimal
    require_ip_restriction: bool
    block_on_key_warnings: bool
    key_max_age: timedelta
    ttl: timedelta


@dataclass(frozen=True, slots=True)
class ActivationInputs:
    now: datetime
    run_id: str | None
    no_test: bool
    eligible: bool
    gate_approved: bool
    metrics: PaperMetrics
    daily_loss_limit: Decimal
    phrase: str | None
    confirm_real_money: bool
    initial_capital: Decimal | None
    key: KeyFacts | None
    live_run_active: bool
    active_authorization: bool


def evaluate_activation(inputs: ActivationInputs, policy: ActivationPolicy) -> tuple[Unmet, ...]:
    """Todas as condicoes que NAO valem, com o motivo exato; vazio so se TODAS valem."""
    m = inputs.metrics
    found: list[Unmet] = []

    def fail(code: str, message: str) -> None:
        found.append(Unmet(code=code, message=message))

    if inputs.no_test or inputs.run_id is None:
        fail("no_test", "Nao ha teste no Paper com diario para avaliar.")
    elif not inputs.eligible:
        fail("ineligible_run", "O teste rodou com o calendario de eventos aberto e nunca libera o Live.")
    if not inputs.gate_approved:
        fail("gate_not_approved", "A trava de criterios do Paper nao foi aprovada.")
    if m.operating_days < MIN_PAPER_DAYS:
        fail("operating_days", f"{m.operating_days} dias de Paper; minimo {MIN_PAPER_DAYS}.")
    if m.max_drawdown is None or m.max_drawdown >= MAX_DRAWDOWN:
        fail("max_drawdown", f"Drawdown {m.max_drawdown}; precisa ser medido e abaixo de {MAX_DRAWDOWN}.")
    if m.profit_factor is None or m.profit_factor <= MIN_PROFIT_FACTOR:
        fail("profit_factor", f"Profit factor {m.profit_factor}; precisa ser maior que {MIN_PROFIT_FACTOR}.")
    if m.trades < MIN_TRADES:
        fail("min_trades", f"{m.trades} operacoes; minimo {MIN_TRADES}.")
    if m.losing_trades < MIN_LOSSES:
        fail("min_losses", f"{m.losing_trades} perdas na amostra; minimo {MIN_LOSSES}.")
    daily_limit = effective_daily_loss_limit(inputs.daily_loss_limit)
    if m.worst_daily_loss_pct > daily_limit:
        fail("daily_loss", f"Houve dia com perda {m.worst_daily_loss_pct}; limite {daily_limit}.")
    if inputs.phrase != LIVE_PHRASE:
        fail("phrase", "A frase de confirmacao precisa ser exatamente a exigida.")
    if inputs.confirm_real_money is not True:
        fail("confirm_real_money", "Falta a confirmacao explicita de que o robo operara com dinheiro real.")
    found.extend(_capital_problems(inputs.initial_capital, policy))
    found.extend(_key_problems(inputs.key, inputs.now, policy))
    if inputs.live_run_active:
        fail("live_run_active", "Ja existe um run Live ativo.")
    if inputs.active_authorization:
        fail("authorization_active", "Ja existe uma autorizacao do Live ativa; revogue-a antes.")
    return tuple(found)


def effective_daily_loss_limit(run_limit: Decimal) -> Decimal:
    """Piso fixo: um run Paper configurado com perda diaria frouxa (ex.: 5%) nunca afrouxa a trava do Live."""
    return min(run_limit, MAX_DAILY_LOSS)


def _capital_problems(capital: Decimal | None, policy: ActivationPolicy) -> list[Unmet]:
    if capital is None or not capital.is_finite() or capital <= 0:
        return [
            Unmet(code="initial_capital", message="Informe o capital inicial reduzido do Live (maior que 0).")
        ]
    if capital > policy.initial_capital_cap:
        return [
            Unmet(
                code="initial_capital_above_cap",
                message=f"Capital inicial {capital} acima do teto reduzido {policy.initial_capital_cap}.",
            )
        ]
    return []


def _key_problems(key: KeyFacts | None, now: datetime, policy: ActivationPolicy) -> list[Unmet]:
    if key is None:
        return [Unmet(code="key_missing", message="Nenhuma chave da Binance salva.")]
    found: list[Unmet] = []
    if key.verified_at is None or now - key.verified_at > policy.key_max_age or key.verified_at > now:
        found.append(Unmet(code="key_unverified", message="A chave nao foi verificada recentemente."))
    if not (key.can_read and key.can_trade):
        found.append(Unmet(code="key_cannot_trade", message="A chave precisa ler e operar no Spot."))
    if key.can_withdraw:
        found.append(Unmet(code="key_withdraw", message="A chave tem permissao de saque."))
    if policy.require_ip_restriction and key.ip_restricted is not True:
        found.append(Unmet(code="key_ip_unrestricted", message="A chave nao tem restricao de IP."))
    if policy.block_on_key_warnings and (key.can_transfer or key.margin_enabled or key.futures_enabled):
        found.append(
            Unmet(code="key_warnings", message="A chave tem transferencia, margem ou futuros habilitados.")
        )
    return found


def authorization_hash(record: dict[str, Any]) -> str:
    """SHA-256 do registro canonico (chaves ordenadas, decimais/datas como texto)."""
    text = json.dumps(record, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()


_MAC_DOMAIN: Final = b"cryptoinvest/v1/live_authorization"
MIN_MAC_KEY_BYTES: Final = 32


def authorization_record(
    *,
    authorization_id: str,
    paper_run_id: str,
    criteria: dict[str, Any],
    actor: str,
    reason: str,
    confirmed_phrase: str,
    initial_capital_quote: Decimal,
    credential_id: str,
    key_permissions: dict[str, Any],
    created_at: datetime,
    expires_at: datetime,
) -> dict[str, Any]:
    """Registro canonico coberto pelo MAC. Decimal e datas sao normalizados para que o valor lido do banco
    (Numeric/timestamptz) reproduza exatamente o texto assinado na gravacao."""
    return {
        "id": authorization_id,
        "paper_run_id": paper_run_id,
        "criteria": criteria,
        "actor": actor,
        "reason": reason,
        "confirmed_phrase": confirmed_phrase,
        "initial_capital_quote": format(initial_capital_quote.normalize(), "f"),
        "credential_id": credential_id,
        "key_permissions": key_permissions,
        "created_at": created_at.astimezone(UTC).isoformat(),
        "expires_at": expires_at.astimezone(UTC).isoformat(),
    }


def authorization_mac(key: bytes, record: dict[str, Any]) -> str:
    """HMAC-SHA256 (sub-chave por dominio) do registro: sem o segredo de arquivo nao se forja o `record_hash`
    nem com acesso de escrita ao banco."""
    if len(key) < MIN_MAC_KEY_BYTES:
        raise ValueError("chave do MAC da autorizacao curta demais")
    text = json.dumps(record, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)
    derived = hmac.new(key, _MAC_DOMAIN, hashlib.sha256).digest()
    return hmac.new(derived, text.encode(), hashlib.sha256).hexdigest()


def authorization_mac_valid(key: bytes, record: dict[str, Any], received: object) -> bool:
    """Reverificacao pelo consumidor (runner Live), em tempo constante; divergencia = autorizacao invalida."""
    if not isinstance(received, str) or not received.isascii() or len(key) < MIN_MAC_KEY_BYTES:
        return False
    return hmac.compare_digest(authorization_mac(key, record).encode(), received.encode())


class LiveStage(StrEnum):
    NO_AUTHORIZATION = "no_authorization"
    AUTHORIZED_WAITING_START = "authorized_waiting_start"
    REDUCED_CAPITAL = "reduced_capital"
    OBSERVATION = "observation"
    LIMIT_INCREASE_AVAILABLE = "limit_increase_available"
    LIMIT_INCREASE_BLOCKED = "limit_increase_blocked"


@dataclass(frozen=True, slots=True)
class StageState:
    stage: LiveStage
    days_in_live: int
    limit_increase_allowed: bool
    next_step: str


def live_stage(
    *,
    authorized: bool,
    live_started_at: datetime | None,
    live_metrics: PaperMetrics | None,
    daily_loss_limit: Decimal,
    now: datetime,
) -> StageState:
    """Etapa calculada das metricas do Live: capital reduzido nos primeiros 7 dias; aumento so apos 30."""
    if not authorized:
        return StageState(LiveStage.NO_AUTHORIZATION, 0, False, "Sem autorizacao ativa do Live.")
    if live_started_at is None:
        return StageState(
            LiveStage.AUTHORIZED_WAITING_START,
            0,
            False,
            "Autorizado; o Live ainda nao iniciou (capital reduzido).",
        )
    days = max(0, (now - live_started_at).days)
    if days < REDUCED_CAPITAL_DAYS:
        return StageState(
            LiveStage.REDUCED_CAPITAL,
            days,
            False,
            f"Capital reduzido por {REDUCED_CAPITAL_DAYS} dias; faltam {REDUCED_CAPITAL_DAYS - days}.",
        )
    if days < LIMIT_INCREASE_DAYS:
        return StageState(
            LiveStage.OBSERVATION,
            days,
            False,
            f"Aumento de limites so apos {LIMIT_INCREASE_DAYS} dias no Live; "
            f"faltam {LIMIT_INCREASE_DAYS - days}.",
        )
    within = (
        live_metrics is not None
        and live_metrics.max_drawdown is not None
        and live_metrics.max_drawdown < MAX_DRAWDOWN
        and live_metrics.worst_daily_loss_pct <= effective_daily_loss_limit(daily_loss_limit)
        and live_metrics.total_pnl >= 0
    )
    if within:
        return StageState(
            LiveStage.LIMIT_INCREASE_AVAILABLE, days, True, "Aumento de limites pode ser pedido."
        )
    return StageState(
        LiveStage.LIMIT_INCREASE_BLOCKED,
        days,
        False,
        "30 dias cumpridos, mas as metricas do Live estao fora das metas "
        "(drawdown, perda diaria, resultado).",
    )
