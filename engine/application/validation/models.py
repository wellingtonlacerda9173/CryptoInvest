from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field, computed_field

from engine.application.records import Record
from engine.application.regime import RegimeConfig
from engine.application.selector import SelectionDecision, SelectorConfig


class Verdict(StrEnum):
    SUPERA = "supera as referências fora da amostra"
    NAO_DISTINGUIVEL = "não distinguível das referências"
    PIOR = "pior que as referências"
    AMOSTRA_INSUFICIENTE = "amostra insuficiente"
    CENSURADO = "censurado"


class ValidationConfig(Record):
    """`train_days_min` de historico antes da 1a janela de teste; treino expansivo (rolante com `train_days`).

    Referencias nulas: `simulations` sorteios de entradas aleatorias com a mesma contagem (por janela), mesmo
    stop e alvo de cada trade real, semente fixa; `cost_pct` (ida e volta) vem das taxas/slippage do
    backtest. `alpha` e corrigido pelas `comparisons` (Sidak/Bonferroni).
    """

    train_days_min: int = Field(default=180, ge=7)
    test_days: int = Field(default=60, ge=1)
    train_days: int | None = Field(default=None, ge=7)
    selector: SelectorConfig = SelectorConfig()
    regime: RegimeConfig = RegimeConfig()
    simulations: int = Field(default=10_000, ge=50)
    max_idle_fraction: float = Field(default=0.30, gt=0, lt=1)
    seed: int = 20260101
    alpha: float = Field(default=0.05, gt=0, lt=1)
    correction: Literal["sidak", "bonferroni"] = "sidak"
    extra_comparisons: int = Field(default=0, ge=0)
    horizon_candles: int = Field(default=72, ge=1)
    cost_pct: float | None = Field(default=None, ge=0)


class TradeStats(Record):
    trades: int
    mean_r: float | None
    total_r: float
    max_drawdown_r: float
    total_return: float
    max_drawdown: float
    win_rate: float | None


class BuyHold(Record):
    total_return: float
    max_drawdown: float
    candles: int

    @computed_field  # type: ignore[prop-decorator]
    @property
    def return_over_drawdown(self) -> float | None:
        return self.total_return / self.max_drawdown if self.max_drawdown > 0 else None


class RandomReference(Record):
    simulations: int
    seed: int
    mean_r_p05: float
    mean_r_median: float
    mean_r_p95: float
    real_mean_r: float
    p_better: float
    p_worse: float


class Judgement(Record):
    verdict: Verdict
    p_better: float | None
    p_worse: float | None
    alpha: float
    alpha_corrected: float
    comparisons: int
    correction: str
    beats_buy_hold: bool | None
    min_attainable_p: float = 1.0
    underpowered: bool = False
    notes: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return self.verdict is Verdict.SUPERA


class EntryReport(Record):
    name: str
    stats: TradeStats
    random_reference: RandomReference | None
    judgement: Judgement
    idle_fraction: float | None = None
    exposure: float | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def return_per_exposure(self) -> float | None:
        return self.stats.total_return / self.exposure if self.exposure else None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def return_over_drawdown(self) -> float | None:
        return self.stats.total_return / self.stats.max_drawdown if self.stats.max_drawdown > 0 else None


class WindowReport(Record):
    index: int
    as_of: datetime
    test_start: datetime
    test_end: datetime
    decisions: tuple[SelectionDecision, ...]
    selector_trades: int
    selector_total_r: float
    idle_regimes: int


class ValidationReport(Record):
    symbol: str
    timeframe: str
    period_start: datetime
    period_end: datetime
    generated_at: datetime
    config: ValidationConfig
    comparisons: int
    min_trades: int
    windows: tuple[WindowReport, ...]
    buy_hold: BuyHold
    strategies: tuple[EntryReport, ...]
    selector: EntryReport
