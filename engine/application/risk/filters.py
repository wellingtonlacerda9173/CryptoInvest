import json
from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Literal, Self, get_args

from pydantic import Field, model_validator

from engine.application.risk.rejections import Rejection, RejectionCode
from engine.domain.base import DomainModel
from engine.domain.candle import Candle
from engine.domain.risk import RiskConfig
from engine.domain.types import Price, Symbol, Timeframe, UtcDatetime, Volume

RunMode = Literal["live", "backtest"]

STALE_AFTER_TIMEFRAMES = 2
QUOTE_CLOCK_SKEW = timedelta(seconds=5)


class MarketSnapshot(DomainModel):
    symbol: Symbol
    at: UtcDatetime
    bid: Price
    ask: Price
    quote_volume_24h: Volume

    @model_validator(mode="after")
    def _check_book(self) -> Self:
        if self.ask < self.bid:
            raise ValueError("ask must be >= bid")
        return self

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> Decimal:
        return (self.ask - self.bid) / self.mid


class EventWindow(DomainModel):
    start: UtcDatetime
    end: UtcDatetime
    label: str = Field(default="evento", min_length=1)

    @model_validator(mode="after")
    def _check_order(self) -> Self:
        if self.end < self.start:
            raise ValueError("end must not precede start")
        return self


class EventCalendar(DomainModel):
    """Janelas UTC de eventos de alta volatilidade (ex.: FOMC, CPI) com cobertura declarada.

    Sem `loaded_at` e `valid_until` o calendario nao tem cobertura e o gate recusa entradas (falha fechada).
    Um calendario sem janelas so vale se o usuario o confirma com `acknowledged_no_events=true`.
    """

    loaded_at: UtcDatetime | None = None
    valid_until: UtcDatetime | None = None
    acknowledged_no_events: bool = False
    windows: tuple[EventWindow, ...] = ()

    @model_validator(mode="after")
    def _check_coverage_order(self) -> Self:
        if self.loaded_at is not None and self.valid_until is not None and self.valid_until < self.loaded_at:
            raise ValueError("valid_until must not precede loaded_at")
        return self

    @classmethod
    def from_json(cls, text: str) -> Self:
        return cls.model_validate(json.loads(text))

    @classmethod
    def load(cls, path: Path) -> Self:
        return cls.from_json(path.read_text(encoding="utf-8"))

    def coverage_problem(self, now: datetime) -> str | None:
        if self.loaded_at is None or self.valid_until is None:
            return "calendario sem cobertura declarada (loaded_at/valid_until ausentes)"
        if self.loaded_at > now:
            return "calendario carregado em data futura"
        if now > self.valid_until:
            return f"calendario vencido em {self.valid_until.isoformat()}"
        if not self.windows and not self.acknowledged_no_events:
            return "calendario vazio sem confirmacao explicita (acknowledged_no_events)"
        return None

    def active_at(self, now: datetime, before: timedelta, after: timedelta) -> EventWindow | None:
        return next((w for w in self.windows if w.start - before <= now <= w.end + after), None)


def history_problem(
    history: Sequence[Candle], symbol: str, timeframe: Timeframe, now: datetime, min_candles: int
) -> str | None:
    """Historico sem buracos, ordenado, fechado e recente; qualquer falha significa 'nao opera'."""
    if not history:
        return "historico vazio"
    if len(history) < min_candles:
        return f"historico com {len(history)} velas; minimo {min_candles}"
    step = timedelta(seconds=timeframe.seconds)
    for previous, current in pairwise(history):
        if current.open_time - previous.open_time != step:
            return (
                f"buraco ou desordem entre {previous.open_time.isoformat()} e {current.open_time.isoformat()}"
            )
    if any(c.symbol != symbol or c.timeframe is not timeframe or not c.closed for c in history):
        return "historico com simbolo, timeframe ou velas nao fechadas inconsistentes"
    if history[-1].close_time > now:
        return "historico com vela futura"
    if now - history[-1].close_time > step * STALE_AFTER_TIMEFRAMES:
        return "historico desatualizado"
    return None


def check_market(
    market: MarketSnapshot,
    calendar: EventCalendar,
    config: RiskConfig,
    now: datetime,
    *,
    mode: RunMode = "live",
) -> Rejection | None:
    """`mode="backtest"` dispensa APENAS a cobertura do calendario; ao vivo ela e obrigatoria."""
    if mode not in get_args(RunMode):
        raise ValueError(f"mode must be one of {get_args(RunMode)}, got {mode!r}")
    age = now - market.at
    if age > timedelta(seconds=config.max_quote_age_seconds) or age < -QUOTE_CLOCK_SKEW:
        return Rejection(
            code=RejectionCode.QUOTE_STALE,
            message=f"cotacao de {market.at.isoformat()} fora da janela de {config.max_quote_age_seconds}s",
        )
    if market.spread > config.max_spread:
        return Rejection(
            code=RejectionCode.SPREAD_TOO_WIDE,
            message=f"spread {market.spread:.5f} acima do maximo {config.max_spread}",
        )
    if market.quote_volume_24h < config.min_quote_volume_24h:
        return Rejection(
            code=RejectionCode.LOW_LIQUIDITY,
            message=f"volume 24h {market.quote_volume_24h} abaixo do minimo {config.min_quote_volume_24h}",
        )
    if mode == "live" and (problem := calendar.coverage_problem(now)) is not None:
        return Rejection(code=RejectionCode.EVENT_CALENDAR_STALE, message=problem)
    before = timedelta(minutes=config.event_margin_before_minutes)
    after = timedelta(minutes=config.event_margin_after_minutes)
    if (window := calendar.active_at(now, before, after)) is not None:
        return Rejection(
            code=RejectionCode.EVENT_WINDOW, message=f"janela de evento de volatilidade: {window.label}"
        )
    return None
