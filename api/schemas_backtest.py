"""Contrato dos backtests (jobs executados fora do processo da API)."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StringConstraints, model_validator

from engine.domain.types import Timeframe

JobStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]
StrategyName = Literal["elephant", "ema"]


def _normalize_pair(value: object) -> object:
    return value.strip().upper() if isinstance(value, str) else value


PairText = Annotated[
    str,
    BeforeValidator(_normalize_pair),
    StringConstraints(pattern=r"^[A-Z0-9]{2,12}/[A-Z0-9]{2,12}$"),
]
PARAMS = ("min_confidence", "rsi_min", "reward_risk")
# faixas aceitas por parametro (as das estrategias; reward_risk nunca abaixo do minimo do risco)
PARAM_RANGES: dict[str, tuple[Decimal, Decimal]] = {
    "min_confidence": (Decimal(0), Decimal(1)),
    "rsi_min": (Decimal(0), Decimal(99)),
    "reward_risk": (Decimal(2), Decimal(10)),
}
# a Binance Spot so tem dados a partir daqui: antes disso o backtest nao teria o que simular
MIN_START = datetime(2017, 8, 17, tzinfo=UTC)


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BacktestCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbols: list[PairText] = Field(min_length=1, max_length=4, examples=[["BTC/USDT"]])
    timeframe: Timeframe = Timeframe.H1
    years: float | None = Field(
        default=None,
        gt=0,
        le=20,
        allow_inf_nan=False,
        description="Período recente (alternativa a start/end).",
    )
    start: datetime | None = None
    end: datetime | None = None
    strategies: list[StrategyName] = Field(default=["elephant", "ema"], min_length=1, max_length=2)
    params: dict[str, Decimal] = Field(default_factory=dict, description=f"Chaves aceitas: {PARAMS}.")
    capital: Decimal = Field(default=Decimal(10_000), gt=0, le=Decimal(100_000_000))
    taker_fee: Decimal = Field(default=Decimal("0.001"), ge=0, le=Decimal("0.01"))
    maker_fee: Decimal = Field(default=Decimal("0.001"), ge=0, le=Decimal("0.01"))
    slippage_bps: Decimal = Field(default=Decimal(5), ge=0, le=Decimal(500))
    latency_candles: int = Field(default=1, ge=1, le=10)
    walk_forward: bool = False
    train_days: int = Field(default=180, ge=7, le=1500)
    test_days: int = Field(default=60, ge=1, le=720)
    grid: str | None = Field(default=None, max_length=300, examples=["min_confidence=0.5,0.6;rsi_min=45,50"])

    @model_validator(mode="after")
    def _period(self) -> Self:
        if self.years is None and self.start is None and self.end is None:
            self.years = 1.0
        if self.years is not None and (self.start is not None or self.end is not None):
            raise ValueError("informe `years` OU `start` e `end`, não os dois")
        if (self.start is None) != (self.end is None) and self.years is None:
            raise ValueError("informe `start` e `end` juntos")
        if self.start is not None and self.end is not None:
            if self.start.tzinfo is None or self.end.tzinfo is None:
                raise ValueError("`start` e `end` precisam de fuso horário")
            if self.start >= self.end:
                raise ValueError("`start` deve ser anterior a `end`")
        if unknown := sorted(set(self.params) - set(PARAMS)):
            raise ValueError(f"parâmetros desconhecidos {unknown}; válidos: {list(PARAMS)}")
        if len(set(self.symbols)) != len(self.symbols):
            raise ValueError("pares repetidos")
        if len(set(self.strategies)) != len(self.strategies):
            raise ValueError("estratégias repetidas")
        for name, value in self.params.items():
            low, high = PARAM_RANGES[name]
            if not value.is_finite() or not low <= value <= high:
                raise ValueError(f"`{name}` fora da faixa permitida ({low} a {high})")
        if self.start is not None and self.start.astimezone(UTC) < MIN_START:
            raise ValueError(f"`start` não pode ser anterior a {MIN_START:%Y-%m-%d} (início dos dados)")
        return self


class ProgressOut(_Out):
    stage: str = ""
    percent: float = 0
    message: str = ""
    symbol: str | None = None
    updated_at: datetime | None = None


class BacktestJobOut(_Out):
    id: str
    status: JobStatus
    created_by: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    cancel_requested: bool
    params: dict[str, Any]
    progress: ProgressOut
    error: str | None
    has_report: bool


class BacktestPage(_Out):
    items: list[BacktestJobOut]
    next_cursor: str | None = None


class BacktestReportOut(_Out):
    job_id: str
    mode: Literal["backtest"] = "backtest"
    generated_at: datetime | None
    disclaimer: str
    assumptions: dict[str, Any]
    results: list[dict[str, Any]]
