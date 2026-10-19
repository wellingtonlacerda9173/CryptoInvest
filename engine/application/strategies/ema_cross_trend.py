from collections.abc import Sequence
from typing import Self

from pydantic import Field, model_validator

from engine.application.indicators import CandleArrays, adx, atr, ema, rsi, volume_average
from engine.application.strategies.base import MIN_CONFLUENCES, StrategyContext, StrategySignal
from engine.application.strategies.confluence import score_confluences
from engine.application.strategies.continuity import candles_are_continuous
from engine.application.strategies.levels import (
    LevelsConfig,
    StopMode,
    compute_levels,
    effective_fee_rate,
    htf_confluence,
)
from engine.domain.base import DomainModel
from engine.domain.candle import Candle
from engine.domain.signal import Signal


class EmaCrossTrendConfig(DomainModel):
    levels: LevelsConfig = LevelsConfig(stop_mode=StopMode.ATR)
    fast_period: int = Field(default=9, ge=2)
    slow_period: int = Field(default=21, ge=3)
    trend_period: int = Field(default=50, ge=4)
    adx_period: int = Field(default=14, ge=2)
    adx_min: float = Field(default=20.0, gt=0, le=100)
    atr_period: int = Field(default=14, ge=2)
    volume_period: int = Field(default=20, ge=2)
    rsi_period: int = Field(default=14, ge=2)
    rsi_range: tuple[float, float] = (50.0, 70.0)
    structure_lookback: int = Field(default=10, ge=2)
    continuity_window: int = Field(default=100, ge=2)
    min_confluences: int = Field(default=MIN_CONFLUENCES, ge=MIN_CONFLUENCES)
    min_confidence: float = Field(default=0.5, ge=0, le=1)

    @model_validator(mode="after")
    def _check_periods(self) -> Self:
        if not self.fast_period < self.slow_period:
            raise ValueError("fast_period must be < slow_period")
        low, high = self.rsi_range
        if not 0 <= low < high <= 100:
            raise ValueError("rsi_range must satisfy 0 <= low < high <= 100")
        return self


TRIGGERS = ("cruzamento_ema_para_cima", "adx_em_tendencia")


class EmaCrossTrend:
    """Cruzamento da EMA rapida sobre a lenta na ultima vela, exigindo tendencia (ADX) como filtro."""

    name = "ema_cross_trend"

    def __init__(self, config: EmaCrossTrendConfig | None = None) -> None:
        self.config = config or EmaCrossTrendConfig()

    def evaluate(self, candles: Sequence[Candle], context: StrategyContext) -> StrategySignal | None:
        cfg = self.config
        needed = (
            max(cfg.trend_period, cfg.slow_period, cfg.adx_period * 2, cfg.atr_period + 1, cfg.volume_period)
            + 2
        )
        if len(candles) < needed:
            return None
        last = candles[-1]
        if not all(c.closed for c in candles) or not candles_are_continuous(
            candles, last.timeframe, max(cfg.continuity_window, needed)
        ):
            return None

        data = CandleArrays.from_candles(candles)
        fast = ema(data.close, cfg.fast_period)
        slow = ema(data.close, cfg.slow_period)
        crossed_up = bool(fast[-2] <= slow[-2] and fast[-1] > slow[-1])
        directional = adx(data.high, data.low, data.close, cfg.adx_period)
        if not crossed_up or not directional.adx[-1] >= cfg.adx_min:
            return None

        atr_value = float(atr(data.high, data.low, data.close, cfg.atr_period)[-1])
        recent = candles[-cfg.structure_lookback :]
        levels = compute_levels(
            last.close,
            atr_value,
            min(c.low for c in recent),
            cfg.levels,
            effective_fee_rate(cfg.levels, context),
        )
        if levels is None:
            return None
        stop, target = levels

        rsi_low, rsi_high = cfg.rsi_range
        rsi_now = float(rsi(data.close, cfg.rsi_period)[-1])
        avg_volume = volume_average(data.volume, cfg.volume_period)[-1]
        recent_high = max(c.high for c in candles[-cfg.structure_lookback - 1 : -1])
        checks = {
            "volume_acima_da_media": bool(data.volume[-1] >= avg_volume),
            "preco_acima_da_ema_de_tendencia": bool(data.close[-1] > ema(data.close, cfg.trend_period)[-1]),
            "dominio_comprador_di": bool(directional.plus_di[-1] > directional.minus_di[-1]),
            "rsi_em_zona_de_forca": rsi_low <= rsi_now <= rsi_high,
            "fechamento_acima_da_maxima_recente": last.close > recent_high,
            "tendencia_superior_de_alta": htf_confluence(context),
        }
        hits, families, confidence = score_confluences(checks)
        if len(families) < cfg.min_confluences or confidence < cfg.min_confidence:
            return None

        signal = Signal(
            symbol=last.symbol,
            timeframe=last.timeframe,
            created_at=last.close_time,
            strategy=self.name,
            entry=last.close,
            stop_loss=stop,
            take_profit=target,
            min_reward_risk=cfg.levels.reward_risk,
        )
        rationale = (
            f"EMA{cfg.fast_period} cruzou acima da EMA{cfg.slow_period} com ADX {directional.adx[-1]:.1f} "
            f"(minimo {cfg.adx_min:g}). Confluencias independentes ({len(families)} familias): "
            f"{', '.join(hits)}."
        )
        return StrategySignal(
            signal=signal, confidence=confidence, rationale=rationale, triggers=TRIGGERS, confluences=hits
        )
