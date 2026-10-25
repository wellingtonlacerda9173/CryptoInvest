"""Compara documentos de configuração: o que mudou, se AUMENTA o risco e quanto custa, em linguagem simples.

Regra de segurança: só reduções de risco podem ser aplicadas a um robô em execução; tudo que aumenta o risco
(ou cujo efeito não se pode julgar) só vale no próximo (re)início, sem posição aberta.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from pydantic import ValidationError

from engine.application.trading_config.document import ConfigDocument
from engine.domain.risk import RiskConfig

PCT_FIELDS = frozenset(
    {
        "risk_per_trade",
        "max_daily_loss",
        "max_position_fraction",
        "daily_profit_target",
        "max_drawdown",
        "max_coin_exposure_fraction",
        "max_total_exposure_fraction",
        "max_spread",
        "taker_fee_rate",
        "max_entry_deviation",
        "partial_exit_fraction",
        "profit_giveback",
    }
)


class Effect(StrEnum):
    INCREASE = "increase"
    DECREASE = "decrease"
    NEUTRAL = "neutral"
    UNKNOWN = "unknown"


# "up": valor maior = mais risco; "down": valor menor = mais risco; ausente: incerto (conta como aumento)
RISK_DIRECTION: dict[str, str] = {
    "risk_per_trade": "up",
    "max_open_positions": "up",
    "max_daily_loss": "up",
    "max_position_fraction": "up",
    "daily_profit_target": "up",
    "max_drawdown": "up",
    "consecutive_loss_limit": "up",
    "max_coin_exposure_fraction": "up",
    "max_total_exposure_fraction": "up",
    "max_spread": "up",
    "max_entry_fee_multiple": "up",
    "max_signal_age_timeframes": "up",
    "max_entry_deviation": "up",
    "max_quote_age_seconds": "up",
    "min_reward_risk": "down",
    "pair_pause_hours": "down",
    "min_quote_volume_24h": "down",
    "event_margin_before_minutes": "down",
    "event_margin_after_minutes": "down",
    "min_history_candles": "down",
    "blocked_assets": "blocked",
    "allowed_assets": "allowed",
}

LABELS: dict[str, str] = {
    "risk_per_trade": "Risco por operação",
    "min_reward_risk": "Retorno/risco mínimo",
    "max_open_positions": "Posições abertas ao mesmo tempo",
    "max_daily_loss": "Perda diária máxima",
    "max_position_fraction": "Fração do saldo por posição",
    "daily_profit_target": "Meta diária",
    "max_drawdown": "Queda máxima do saldo (drawdown)",
    "consecutive_loss_limit": "Perdas seguidas permitidas",
    "pair_pause_hours": "Pausa do par após perdas seguidas (horas)",
    "max_coin_exposure_fraction": "Exposição máxima por moeda",
    "max_total_exposure_fraction": "Exposição total máxima",
    "max_spread": "Diferença compra/venda máxima",
    "min_quote_volume_24h": "Volume mínimo em 24 h",
    "event_margin_before_minutes": "Margem antes de eventos (min)",
    "event_margin_after_minutes": "Margem depois de eventos (min)",
    "taker_fee_rate": "Taxa da corretora",
    "blocked_assets": "Moedas bloqueadas",
    "allowed_assets": "Moedas permitidas",
    "partial_exit_fraction": "Fração da saída parcial",
    "trailing_r": "Distância do stop móvel (R)",
    "profit_giveback": "Parte do lucro que pode devolver",
    "max_entry_fee_multiple": "Tolerância da taxa na entrada",
    "max_signal_age_timeframes": "Idade máxima do sinal (velas)",
    "max_entry_deviation": "Desvio máximo do preço de entrada",
    "max_quote_age_seconds": "Idade máxima da cotação (s)",
    "min_history_candles": "Velas mínimas de histórico",
    "quote_asset": "Moeda de cotação",
    "fee_asset": "Moeda da taxa",
    "pairs.symbols": "Pares",
    "pairs.timeframe": "Tempo gráfico",
    "strategies.elephant_enabled": "Estratégia Barra elefante",
    "strategies.ema_enabled": "Estratégia Cruzamento de EMAs",
    "strategies.min_confidence": "Confiança mínima",
    "strategies.reward_risk": "Retorno/risco alvo",
    "ai.enabled": "Assistente de IA",
    "ai.model": "Modelo de IA",
    "ai.monthly_budget_usd": "Orçamento mensal da IA (US$)",
    "ai.timeout_seconds": "Prazo de resposta da IA (s)",
}


@dataclass(frozen=True, slots=True)
class FieldChange:
    path: str
    before: Any
    after: Any
    effect: Effect

    @property
    def increases_risk(self) -> bool:
        return self.effect in (Effect.INCREASE, Effect.UNKNOWN)

    @property
    def section(self) -> str:
        return self.path.split(".", 1)[0]

    @property
    def field(self) -> str:
        return self.path.split(".", 1)[1]


def _numeric(*values: Any) -> bool:
    try:
        for v in values:
            if isinstance(v, bool) or not isinstance(v, int | float | str | Decimal):
                return False
            Decimal(str(v))
    except InvalidOperation:
        return False
    return True


def _num(value: Any) -> Decimal:
    return Decimal(str(value))


def _as_set(value: Any) -> frozenset[str]:
    return frozenset(value or ())


def risk_effect(name: str, before: Any, after: Any) -> Effect:
    """Efeito de mudar UM campo de `RiskConfig` sobre o risco assumido."""
    if before == after:
        return Effect.NEUTRAL
    direction = RISK_DIRECTION.get(name)
    if direction in ("up", "down"):
        if not _numeric(before, after):
            return Effect.UNKNOWN
        if _num(after) == _num(before):
            return Effect.NEUTRAL
        higher = _num(after) > _num(before)
        return Effect.INCREASE if higher == (direction == "up") else Effect.DECREASE
    if direction == "blocked":
        return Effect.INCREASE if _as_set(before) - _as_set(after) else Effect.DECREASE
    if direction == "allowed":
        old, new = _as_set(before), _as_set(after)
        if not new and old:
            return Effect.INCREASE
        if not old and new:
            return Effect.DECREASE
        return Effect.INCREASE if new - old else Effect.DECREASE
    return Effect.UNKNOWN


def _strategy_effect(name: str, before: Any, after: Any) -> Effect:
    match name:
        case "elephant_enabled" | "ema_enabled":
            return Effect.INCREASE if after else Effect.DECREASE
        case "min_confidence" | "reward_risk":
            return Effect.INCREASE if _num(after) < _num(before) else Effect.DECREASE
        case _:
            # parametros de deteccao/indicadores mexem em quantas entradas o robo faz: efeito incerto
            return Effect.UNKNOWN


def _pairs_effect(name: str, before: Any, after: Any) -> Effect:
    if name == "symbols":
        return Effect.INCREASE if _as_set(after) - _as_set(before) else Effect.DECREASE
    return Effect.UNKNOWN


def classify(path: str, before: Any, after: Any) -> Effect:
    section, _, name = path.partition(".")
    match section:
        case "risk":
            return risk_effect(name, before, after)
        case "strategies":
            return _strategy_effect(name, before, after)
        case "pairs":
            return _pairs_effect(name, before, after)
        case _:
            return Effect.NEUTRAL


def _flatten(prefix: str, value: Any, out: dict[str, Any]) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            _flatten(f"{prefix}.{key}" if prefix else str(key), item, out)
    else:
        out[prefix] = value


def flatten(document: ConfigDocument) -> dict[str, Any]:
    out: dict[str, Any] = {}
    _flatten("", document.dump(), out)
    return out


def _same(a: Any, b: Any) -> bool:
    if _numeric(a, b):
        return _num(a) == _num(b)
    return bool(a == b)


def diff_documents(old: ConfigDocument, new: ConfigDocument) -> list[FieldChange]:
    before, after = flatten(old), flatten(new)
    changes: list[FieldChange] = []
    for path in sorted(before.keys() | after.keys()):
        was, now = before.get(path), after.get(path)
        if not _same(was, now):
            changes.append(FieldChange(path, was, now, classify(path, was, now)))
    return changes


def fmt_value(path: str, value: Any) -> str:
    name = path.split(".")[-1]
    if isinstance(value, bool):
        return "ligado" if value else "desligado"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) or "nenhum"
    if value is None:
        return "não definido"
    if name in PCT_FIELDS and _numeric(value):
        return f"{_num(value) * 100:.2f}%".replace(".", ",")
    return str(value).replace(".", ",") if isinstance(value, float) else str(value)


def cost_text(change: FieldChange, capital: Decimal) -> str:
    """Quanto a mudança custa numa perda, em frases curtas (só para campos com custo mensurável)."""
    name = change.field if change.section == "risk" else ""
    if name in ("risk_per_trade", "max_daily_loss", "max_drawdown") and _numeric(change.before, change.after):
        before, after = _num(change.before), _num(change.after)
        what = {
            "risk_per_trade": "Cada operação perdida pode custar até",
            "max_daily_loss": "O robô só para no dia depois de perder até",
            "max_drawdown": "O robô só desliga depois de o saldo cair até",
        }[name]
        return (
            f"{what} {fmt_value(change.path, after)} do saldo "
            f"(cerca de {_money(capital * after)} USDT com saldo de {_money(capital, 0)} USDT); "
            f"antes eram {fmt_value(change.path, before)} ({_money(capital * before)} USDT)."
        )
    if name == "max_open_positions":
        return (
            f"Com {change.after} posições ao mesmo tempo, uma sequência de perdas pode custar "
            f"até {change.after} vezes o risco de uma operação de uma só vez."
        )
    if name in ("max_total_exposure_fraction", "max_coin_exposure_fraction", "max_position_fraction"):
        return f"Até {fmt_value(change.path, change.after)} do saldo pode ficar comprado em moedas."
    if change.path == "ai.monthly_budget_usd" and _numeric(change.before, change.after):
        if _num(change.after) > _num(change.before):
            return (
                f"Aumento de custo (não de risco): a IA pode gastar até US$ {_num(change.after)} por mês; "
                f"antes eram US$ {_num(change.before)}. O robô para de chamar a IA ao atingir o teto."
            )
        return "Redução de custo: a IA passa a ter um teto mensal menor."
    if change.effect is Effect.INCREASE:
        return "Esta mudança deixa o robô aceitar mais risco do que hoje."
    if change.effect is Effect.UNKNOWN:
        return "Não dá para medir o efeito no risco; por segurança só vale no próximo início do robô."
    return ""


def _money(value: Decimal, places: int = 2) -> str:
    text = f"{value:,.{places}f}"
    return text.replace(",", "X").replace(".", ",").replace("X", ".")


def describe(change: FieldChange, capital: Decimal) -> dict[str, str]:
    label = LABELS.get(change.path) or LABELS.get(change.field, change.path)
    return {
        "label": label,
        "before_text": fmt_value(change.path, change.before),
        "after_text": fmt_value(change.path, change.after),
        "cost_text": cost_text(change, capital),
    }


def risk_patch(changes: list[FieldChange], new: ConfigDocument) -> dict[str, Any]:
    """Campos de risco cuja mudanca REDUZ o risco: sao os unicos aplicaveis a um robo em execucao."""
    dump = new.dump()["risk"]
    return {c.field: dump[c.field] for c in changes if c.section == "risk" and c.effect is Effect.DECREASE}


def runner_patch(effective: Mapping[str, Any], wanted: Mapping[str, Any]) -> dict[str, Any]:
    """Campos de risco em que `wanted` REDUZ o risco em relacao ao que o robo EM EXECUCAO usa (`effective`, a
    configuracao que o proprio runner reporta), nao em relacao a ultima versao salva: so esses podem valer na
    hora. Aumentos e campos que o runner nao reporta ficam de fora (valem no proximo inicio)."""
    return {
        name: value
        for name, value in wanted.items()
        if name in effective and risk_effect(name, effective[name], value) is Effect.DECREASE
    }


def limit_to_reductions(base: RiskConfig, wanted: RiskConfig) -> RiskConfig:
    """Parte de `base` e adota de `wanted` só o que NÃO aumenta o risco (reduções e campos neutros).

    Usado no (re)início do runner com posição aberta: aumentos ficam para o próximo início sem posição.
    """
    before = base.model_dump(mode="json")
    after = wanted.model_dump(mode="json")
    safe = (Effect.DECREASE, Effect.NEUTRAL)
    merged = {
        name: after[name] if risk_effect(name, value, after[name]) in safe else value
        for name, value in before.items()
    }
    try:
        return RiskConfig.model_validate(merged)
    except ValidationError:
        return base
