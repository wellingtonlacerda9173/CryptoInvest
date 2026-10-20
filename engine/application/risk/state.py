from collections.abc import Mapping
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Self

from pydantic import Field, StrictInt

from engine.application.risk.clock import require_aware, utc_date
from engine.application.risk.frozen import Frozen, FrozenDict
from engine.application.risk.rejections import Rejection, RejectionCode
from engine.domain.base import DomainModel
from engine.domain.risk import RiskConfig
from engine.domain.trade import Trade
from engine.domain.types import StrictDecimal, Symbol, UtcDatetime, Volume


class AuditAction(StrEnum):
    HALTED = "halted"
    REACTIVATED = "reactivated"
    PAUSED = "paused"
    RESUMED = "resumed"
    DAILY_LOSS_PAUSE = "daily_loss_pause"
    DAILY_TARGET_REACHED = "daily_target_reached"
    PAIR_PAUSED = "pair_paused"


class AuditEntry(DomainModel):
    at: UtcDatetime
    action: AuditAction
    detail: str = Field(min_length=1)
    operator: str | None = None


def next_utc_midnight(now: datetime) -> datetime:
    day = utc_date(now) + timedelta(days=1)
    return datetime.combine(day, time(), tzinfo=UTC)


def _require_identified(operator: str, reason: str) -> None:
    if not operator.strip() or not reason.strip():
        raise ValueError("operator and reason are required for manual actions")


class RiskState(DomainModel):
    """Estado do risco, serializavel (`model_dump_json`). Toda transicao devolve um novo estado.

    `ledger` e `audit` sao append-only e os mapeamentos sao imutaveis (mutacao in-place levanta TypeError).
    Todo horario recebido deve ter fuso (naive levanta ValueError).
    """

    equity: Volume
    peak_equity: Volume
    day: date
    day_start_equity: Volume
    day_pnl: StrictDecimal = Decimal(0)
    equity_observed_at: UtcDatetime | None = None
    daily_pause_until: UtcDatetime | None = None
    target_reached_on: date | None = None
    loss_streaks: Annotated[Mapping[Symbol, StrictInt], Frozen] = Field(default_factory=FrozenDict)
    pair_pause_until: Annotated[Mapping[Symbol, UtcDatetime], Frozen] = Field(default_factory=FrozenDict)
    halted: bool = False
    halt_reason: str | None = None
    paused: bool = False
    pause_reason: str | None = None
    ledger: tuple[Trade, ...] = ()
    audit: tuple[AuditEntry, ...] = ()

    @classmethod
    def initial(cls, equity: Decimal, now: datetime) -> Self:
        return cls(equity=equity, peak_equity=equity, day=utc_date(now), day_start_equity=equity)

    @property
    def drawdown(self) -> Decimal:
        return (self.peak_equity - self.equity) / self.peak_equity if self.peak_equity > 0 else Decimal(0)

    def entry_block(self, symbol: str, now: datetime) -> Rejection | None:
        """Primeira trava que impede NOVAS entradas; posicoes abertas continuam geridas normalmente."""
        today = utc_date(now)
        if self.halted:
            return Rejection(code=RejectionCode.ROBOT_HALTED, message=f"robo desligado: {self.halt_reason}")
        if self.paused:
            return Rejection(code=RejectionCode.PAUSED, message=f"robo pausado: {self.pause_reason}")
        if self.daily_pause_until is not None and now < self.daily_pause_until:
            return Rejection(
                code=RejectionCode.DAILY_LOSS_PAUSE,
                message=f"perda diaria atingida; pausa ate {self.daily_pause_until.isoformat()}",
            )
        if self.target_reached_on == today:
            return Rejection(code=RejectionCode.DAILY_TARGET_REACHED, message="meta diaria atingida")
        if (until := self.pair_pause_until.get(symbol)) is not None and now < until:
            return Rejection(
                code=RejectionCode.PAIR_PAUSED, message=f"{symbol} pausado ate {until.isoformat()}"
            )
        return None

    def roll_day(self, now: datetime) -> Self:
        today = utc_date(now)
        if today <= self.day:
            return self
        return self.model_copy(
            update={
                "day": today,
                "day_start_equity": self.equity,
                "day_pnl": Decimal(0),
                "daily_pause_until": None,
            }
        )

    def observe_equity(self, equity: Decimal, now: datetime, config: RiskConfig) -> Self:
        """Capital autoritativo da conta (ex.: apos reconciliacao); queda do dia conta na perda diaria.

        `day_pnl` nunca sobe por aqui (so cai): equity abaixo do inicio do dia vira perda e pode pausar.

        Contrato com `record_trade` (evita contar a mesma perda duas vezes): a observacao e uma foto da
        conta em `now` que JA inclui todo trade com `closed_at <= now`. Esses trades, se registrados
        depois, entram no ledger/sequencias mas NAO movem `equity` nem `day_pnl` de novo. A foto deve ser
        tirada apos a confirmacao dos fills; trades posteriores a `now` somam normalmente.
        Foto mais antiga que a ultima observacao e ignorada (estado inalterado): o relogio nao regride.
        """
        require_aware(now)
        if not equity.is_finite() or equity < 0:
            raise ValueError(f"equity must be a finite non-negative value, got {equity}")
        if self.equity_observed_at is not None and now < self.equity_observed_at:
            return self
        state = self.roll_day(now)
        state = state.model_copy(
            update={
                "equity_observed_at": now,
                "equity": equity,
                "peak_equity": max(state.peak_equity, equity),
                "day_pnl": min(state.day_pnl, equity - state.day_start_equity),
            }
        )
        return state._enforce_limits(now, config)

    def record_trade(self, trade: Trade, now: datetime, config: RiskConfig) -> Self:
        """Registra o resultado LIQUIDO de taxas (append-only) e aplica as travas.

        Idempotente por `trade_id`. Perdas nunca sao reclassificadas; perda so de taxa e perda. Resultado
        nulo (empate) nao conta como perda nem zera a sequencia; so um ganho zera. O patrimonio nunca fica
        negativo: perda maior que o capital zera a conta e desliga o robo (HALT por capital zerado).
        """
        require_aware(now)
        if trade.trade_id is not None and any(t.trade_id == trade.trade_id for t in self.ledger):
            return self
        state = self.roll_day(now)
        pnl = trade.pnl
        streaks = dict(state.loss_streaks)
        pauses = dict(state.pair_pause_until)
        audit = list(state.audit)
        if pnl > 0:
            streaks.pop(trade.symbol, None)
        elif pnl < 0:
            streaks[trade.symbol] = streaks.get(trade.symbol, 0) + 1
            if streaks[trade.symbol] >= config.consecutive_loss_limit:
                until = now + timedelta(hours=config.pair_pause_hours)
                pauses[trade.symbol] = until
                streaks.pop(trade.symbol)
                detail = f"{trade.symbol}: perdas seguidas no limite; pausa ate {until.isoformat()}"
                audit.append(AuditEntry(at=now, action=AuditAction.PAIR_PAUSED, detail=detail))
        already_observed = (
            state.equity_observed_at is not None and trade.closed_at <= state.equity_observed_at
        )
        applied = Decimal(0) if already_observed else pnl
        equity = max(state.equity + applied, Decimal(0))
        state = state.model_copy(
            update={
                "equity": equity,
                "peak_equity": max(state.peak_equity, equity),
                "day_pnl": state.day_pnl + applied,
                "loss_streaks": streaks,
                "pair_pause_until": pauses,
                "ledger": (*state.ledger, trade),
                "audit": tuple(audit),
            }
        )
        return state._enforce_limits(now, config)

    def pause(self, reason: str, now: datetime) -> Self:
        require_aware(now)
        if self.paused:
            return self
        entry = AuditEntry(at=now, action=AuditAction.PAUSED, detail=reason)
        return self.model_copy(update={"paused": True, "pause_reason": reason, "audit": (*self.audit, entry)})

    def resume(self, *, operator: str, reason: str, now: datetime) -> Self:
        """Retoma de uma pausa (ex.: divergencia de reconciliacao) por acao manual auditada."""
        require_aware(now)
        _require_identified(operator, reason)
        entry = AuditEntry(at=now, action=AuditAction.RESUMED, detail=reason, operator=operator)
        return self.model_copy(update={"paused": False, "pause_reason": None, "audit": (*self.audit, entry)})

    def reactivate(self, *, operator: str, reason: str, now: datetime) -> Self:
        """Unico caminho para religar apos o desligamento por drawdown; reinicia o pico no capital atual."""
        require_aware(now)
        if not self.halted:
            raise ValueError("robot is not halted")
        _require_identified(operator, reason)
        if self.equity <= 0:
            raise ValueError("cannot reactivate with zero equity: the account has no capital left to trade")
        entry = AuditEntry(at=now, action=AuditAction.REACTIVATED, detail=reason, operator=operator)
        return self.model_copy(
            update={
                "halted": False,
                "halt_reason": None,
                "peak_equity": self.equity,
                "audit": (*self.audit, entry),
            }
        )

    def _enforce_limits(self, now: datetime, config: RiskConfig) -> Self:
        update: dict[str, Any] = {}
        audit: list[AuditEntry] = []
        loss_limit_hit = self.day_pnl <= -config.max_daily_loss * self.day_start_equity
        if loss_limit_hit and (self.daily_pause_until is None or now >= self.daily_pause_until):
            until = next_utc_midnight(now)
            update["daily_pause_until"] = until
            detail = f"perda do dia {self.day_pnl}; pausa ate {until.isoformat()}"
            audit.append(AuditEntry(at=now, action=AuditAction.DAILY_LOSS_PAUSE, detail=detail))
        target_hit = self.day_pnl >= config.daily_profit_target * self.day_start_equity
        if target_hit and self.target_reached_on != self.day:
            update["target_reached_on"] = self.day
            detail = f"lucro do dia {self.day_pnl}"
            audit.append(AuditEntry(at=now, action=AuditAction.DAILY_TARGET_REACHED, detail=detail))
        state = self.model_copy(update=update | {"audit": (*self.audit, *audit)}) if audit else self
        return state._check_drawdown(now, config)

    def _check_drawdown(self, now: datetime, config: RiskConfig) -> Self:
        if self.halted or (self.equity > 0 and self.drawdown < config.max_drawdown):
            return self
        reason = (
            "capital zerado"
            if self.equity <= 0
            else f"drawdown {self.drawdown:.4f} >= {config.max_drawdown} a partir do pico {self.peak_equity}"
        )
        entry = AuditEntry(at=now, action=AuditAction.HALTED, detail=reason)
        return self.model_copy(update={"halted": True, "halt_reason": reason, "audit": (*self.audit, entry)})
