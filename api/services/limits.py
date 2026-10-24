"""Travas de risco "valor atual de limite" a partir do estado do robô e do `RiskConfig` em uso."""

from datetime import datetime
from decimal import Decimal
from typing import Any

from api.reasons import STATES
from api.schemas_trading import Level, RiskLimitOut
from api.services.fmt import pct
from api.services.runner_status import parse_time
from engine.domain.risk import RiskConfig

WARN_RATIO = 0.6


def _dec(raw: Any, default: Decimal = Decimal(0)) -> Decimal:
    try:
        return Decimal(str(raw))
    except (ArithmeticError, ValueError):
        return default


def _level(ratio: float) -> tuple[Level, str]:
    if ratio >= 1:
        return "bad", "ATIVA"
    if ratio >= WARN_RATIO:
        return "warn", "Atenção"
    return "ok", "Folgado"


def _limit(
    key: str,
    name: str,
    value: float,
    limit: float,
    *,
    unit: str,
    note: str,
    level: Level,
    status: str,
    until: datetime | None = None,
    places: int = 1,
) -> RiskLimitOut:
    shown = pct(value, places) if unit == "%" else f"{value:g}"
    cap = pct(limit, places) if unit == "%" else f"{limit:g}"
    suffix = f" (até {until:%H:%M} UTC)" if until is not None else ""
    return RiskLimitOut(
        key=key,
        name=name,
        value=round(value, 4),
        limit=round(limit, 4),
        unit="%" if unit == "%" else "",
        level=level,
        status_text=status,
        summary=f"{name}: {shown} de {cap} - {status}{suffix}",
        note=note,
        active_until=until,
    )


def build_limits(view: dict[str, Any], risk: RiskConfig, now: datetime) -> list[RiskLimitOut]:
    day_start = _dec(view.get("day_start_equity")) or _dec(view.get("equity"))
    day_pnl = _dec(view.get("day_pnl"))
    loss_pct = float(max(Decimal(0), -day_pnl) / day_start * 100) if day_start > 0 else 0.0
    loss_cap = float(risk.max_daily_loss * 100)
    pause_until = parse_time(view.get("daily_pause_until"))
    paused_today = pause_until is not None and pause_until > now
    level: Level
    level, status = ("bad", "ATIVA") if paused_today else _level(loss_pct / loss_cap if loss_cap else 0)
    limits = [
        _limit(
            "daily_loss",
            "Perda diária",
            loss_pct,
            loss_cap,
            unit="%",
            note="Ao chegar no limite o robô para de abrir operações até 00:00 UTC.",
            level=level,
            status=status,
            until=pause_until if paused_today else None,
        )
    ]
    drawdown = float(_dec(view.get("drawdown")) * 100)
    dd_cap = float(risk.max_drawdown * 100)
    halted = bool(view.get("halted"))
    level, status = ("bad", "DESLIGADO") if halted else _level(drawdown / dd_cap if dd_cap else 0)
    limits.append(
        _limit(
            "drawdown",
            "Queda do saldo (drawdown)",
            drawdown,
            dd_cap,
            unit="%",
            note="Queda desde o ponto mais alto do saldo. No limite o robô desliga e não volta sozinho.",
            level=level,
            status=status,
        )
    )
    streaks: dict[str, int] = {k: int(v) for k, v in (view.get("loss_streaks") or {}).items()}
    pair_pauses = {
        s: until
        for s, raw in (view.get("pair_pause_until") or {}).items()
        if (until := parse_time(raw)) and until > now
    }
    worst_pair, worst = max(streaks.items(), key=lambda kv: kv[1], default=("", 0))
    cap = risk.consecutive_loss_limit
    if pair_pauses:
        pair, until = next(iter(pair_pauses.items()))
        limits.append(
            _limit(
                "loss_streak",
                f"Perdas seguidas ({pair})",
                float(cap),
                float(cap),
                unit="",
                note=f"O par fica em pausa de {risk.pair_pause_hours} h depois de {cap} perdas seguidas.",
                level="bad",
                status="ATIVA",
                until=until,
            )
        )
    else:
        level = "warn" if worst and worst >= cap - 1 else "ok"
        limits.append(
            _limit(
                "loss_streak",
                f"Perdas seguidas ({worst_pair})" if worst_pair else "Perdas seguidas",
                float(worst),
                float(cap),
                unit="",
                note=f"Com {cap} perdas seguidas no mesmo par, ele pausa por {risk.pair_pause_hours} h.",
                level=level,
                status="Atenção" if level == "warn" else "Folgado",
            )
        )
    open_count = len(view.get("open_positions") or [])
    limits.append(
        _limit(
            "open_positions",
            "Posições abertas",
            float(open_count),
            float(risk.max_open_positions),
            unit="",
            note="Quantas operações podem ficar abertas ao mesmo tempo.",
            level="warn" if open_count >= risk.max_open_positions else "ok",
            status="No limite" if open_count >= risk.max_open_positions else "Folgado",
        )
    )
    exposure = float(_dec(view.get("exposure")) * 100)
    exp_cap = float(risk.max_total_exposure_fraction * 100)
    level, status = _level(exposure / exp_cap if exp_cap else 0)
    limits.append(
        _limit(
            "total_exposure",
            "Exposição total",
            exposure,
            exp_cap,
            unit="%",
            note="Parte do saldo que está comprada em moedas agora.",
            level="warn" if level == "bad" else level,
            status="No limite" if level == "bad" else status,
        )
    )
    gain_pct = float(max(Decimal(0), day_pnl) / day_start * 100) if day_start > 0 else 0.0
    target = float(risk.daily_profit_target * 100)
    reached = view.get("target_reached_on") == view.get("day") and view.get("target_reached_on") is not None
    limits.append(
        _limit(
            "daily_target",
            "Meta do dia",
            gain_pct,
            target,
            unit="%",
            note="Ao atingir a meta, o robô para de abrir operações para proteger o ganho.",
            level="ok",
            status="Atingida" if reached else "Em andamento",
        )
    )
    return limits


def state_words(state: str) -> tuple[str, str]:
    return STATES.get(state, STATES["OFFLINE"])
