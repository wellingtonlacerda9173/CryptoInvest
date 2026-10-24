from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from engine.application.records import Record


class Regime(StrEnum):
    TENDENCIA_ALTA = "tendencia_alta"
    TENDENCIA_BAIXA = "tendencia_baixa"
    LATERAL = "lateral"
    ALTA_VOLATILIDADE = "alta_volatilidade"
    BAIXA_LIQUIDEZ = "baixa_liquidez"
    INDEFINIDO = "indefinido"

    @property
    def label(self) -> str:
        return _LABELS[self]


_LABELS = {
    Regime.TENDENCIA_ALTA: "tendência de alta",
    Regime.TENDENCIA_BAIXA: "tendência de baixa",
    Regime.LATERAL: "lateral",
    Regime.ALTA_VOLATILIDADE: "alta volatilidade",
    Regime.BAIXA_LIQUIDEZ: "baixa liquidez",
    Regime.INDEFINIDO: "indefinido",
}

PROTECTIVE_REGIMES = frozenset({Regime.ALTA_VOLATILIDADE, Regime.BAIXA_LIQUIDEZ})


class RegimeConfig(Record):
    """Limiares PRE-FIXADOS (convencoes classicas; nao ajustados aos dados). Relacoes (`ratio`) comparam o
    valor atual com a media das ultimas `baseline_lookback` velas, o que torna o limiar independente do par
    e do timeframe. `adx_lateral_max < adx_trend_min` deixa uma faixa cinzenta que vira INDEFINIDO.
    """

    adx_period: int = Field(default=14, ge=2)
    atr_period: int = Field(default=14, ge=2)
    ema_fast: int = Field(default=20, ge=2)
    ema_slow: int = Field(default=50, ge=3)
    bb_period: int = Field(default=20, ge=2)
    bb_std: float = Field(default=2.0, gt=0)
    volume_short: int = Field(default=5, ge=1)
    volume_long: int = Field(default=50, ge=2)
    baseline_lookback: int = Field(default=100, ge=10)
    slope_lookback: int = Field(default=10, ge=2)
    range_lookback: int = Field(default=40, ge=10)

    adx_trend_min: float = Field(default=25.0, gt=0)
    adx_lateral_max: float = Field(default=20.0, gt=0)
    slope_min_atr: float = Field(default=0.03, ge=0)
    high_vol_atr_ratio: float = Field(default=1.8, gt=1)
    high_vol_bbw_ratio: float = Field(default=2.2, gt=1)
    low_volume_ratio: float = Field(default=0.35, gt=0, lt=1)
    range_min_height_atr: float = Field(default=3.0, gt=0)
    range_max_height_atr: float = Field(default=14.0, gt=0)
    range_min_touches: int = Field(default=2, ge=1)
    range_touch_fraction: float = Field(default=0.15, gt=0, lt=0.5)
    confirm_bars: int = Field(default=3, ge=1)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.adx_lateral_max >= self.adx_trend_min:
            raise ValueError("adx_lateral_max must be < adx_trend_min")
        if self.range_min_height_atr >= self.range_max_height_atr:
            raise ValueError("range_min_height_atr must be < range_max_height_atr")
        if self.ema_fast >= self.ema_slow:
            raise ValueError("ema_fast must be < ema_slow")
        return self

    @property
    def min_history(self) -> int:
        """Velas necessarias para a primeira leitura valida."""
        return (
            max(
                self.baseline_lookback + self.atr_period,
                2 * self.adx_period,
                self.ema_slow + self.slope_lookback,
                self.volume_long,
                self.range_lookback,
                self.bb_period + self.baseline_lookback,
            )
            + 1
        )


class RegimeMetrics(Record):
    adx: float
    plus_di: float
    minus_di: float
    atr_rel: float
    atr_ratio: float
    bb_width: float
    bb_width_ratio: float
    ema_slope_atr: float
    ema_fast_above_slow: bool
    volume_ratio: float
    range_height_atr: float | None = None
    range_touches_top: int | None = None
    range_touches_bottom: int | None = None


class RegimeReading(Record):
    """Leitura de uma vela fechada: `regime` e o CONFIRMADO (com histerese); `raw_regime`, o da vela."""

    at: datetime
    regime: Regime
    raw_regime: Regime
    range_defined: bool
    operable: bool
    reason: str
    bars_in_regime: int
    pending_regime: Regime | None = None
    pending_bars: int = 0
    metrics: RegimeMetrics | None = None

    def to_context(self) -> dict[str, object]:
        return self.model_dump(mode="json", exclude={"at"})
