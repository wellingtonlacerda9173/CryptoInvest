from collections.abc import Sequence
from typing import Self

import numpy as np
from pydantic import Field, model_validator

from engine.application.detectors import (
    ElephantBarConfig,
    ElephantContext,
    ElephantDirection,
    detect_elephant_bar,
)
from engine.application.indicators import CandleArrays, adx, ema, rsi, swing_highs
from engine.application.strategies.base import MIN_CONFLUENCES, StrategyContext, StrategySignal
from engine.application.strategies.confluence import score_confluences
from engine.application.strategies.continuity import candles_are_continuous
from engine.application.strategies.levels import (
    LevelsConfig,
    compute_levels,
    effective_fee_rate,
    htf_confluence,
)
from engine.domain.base import DomainModel
from engine.domain.candle import Candle
from engine.domain.signal import Signal


class ElephantBarBreakoutConfig(DomainModel):
    elephant: ElephantBarConfig = ElephantBarConfig()
    levels: LevelsConfig = LevelsConfig()
    swing_left: int = Field(default=3, ge=1)
    swing_right: int = Field(default=3, ge=1)
    resistance_lookback: int = Field(default=50, ge=5)
    strong_volume_multiple: float = Field(default=2.0, gt=0)
    trend_ema_period: int = Field(default=50, ge=2)
    adx_period: int = Field(default=14, ge=2)
    rsi_period: int = Field(default=14, ge=2)
    rsi_min: float = Field(default=50.0, ge=0, lt=100)
    rsi_max: float = Field(default=75.0, gt=0, le=100)
    continuity_window: int = Field(default=100, ge=2)
    min_confluences: int = Field(default=MIN_CONFLUENCES, ge=MIN_CONFLUENCES)
    min_confidence: float = Field(default=0.5, ge=0, le=1)

    @model_validator(mode="after")
    def _check_thresholds(self) -> Self:
        if not self.strong_volume_multiple > self.elephant.volume_multiple:
            raise ValueError("strong_volume_multiple must exceed the detector volume_multiple")
        if not self.rsi_min < self.rsi_max:
            raise ValueError("rsi_min must be < rsi_max")
        return self


TRIGGERS = ("barra_elefante_alta", "rompimento_de_resistencia")


class ElephantBarBreakout:
    """Barra elefante de alta fechando acima da resistencia (swing high) mais proxima, com volume."""

    name = "elephant_bar_breakout"

    def __init__(self, config: ElephantBarBreakoutConfig | None = None) -> None:
        self.config = config or ElephantBarBreakoutConfig()

    def evaluate(self, candles: Sequence[Candle], context: StrategyContext) -> StrategySignal | None:
        cfg = self.config
        needed = max(cfg.elephant.min_history, cfg.resistance_lookback, cfg.trend_ema_period) + 1
        if len(candles) < needed:
            return None
        last = candles[-1]
        if not all(c.closed for c in candles) or not candles_are_continuous(
            candles, last.timeframe, max(cfg.continuity_window, needed)
        ):
            return None

        bar = detect_elephant_bar(candles, cfg.elephant)
        if bar is None or bar.direction is not ElephantDirection.BULLISH or bar.atr is None:
            return None

        data = CandleArrays.from_candles(candles)
        resistance = _nearest_resistance(data, cfg)
        if resistance is None or data.close[-1] <= resistance:
            return None

        levels = compute_levels(
            last.close, bar.atr, last.low, cfg.levels, effective_fee_rate(cfg.levels, context)
        )
        if levels is None:
            return None
        stop, target = levels

        directional = adx(data.high, data.low, data.close, cfg.adx_period)
        trend_ema = float(ema(data.close, cfg.trend_ema_period)[-1])
        rsi_now = float(rsi(data.close, cfg.rsi_period)[-1])
        if rsi_now >= cfg.rsi_max or ElephantContext.EXHAUSTION in bar.contexts:
            return None
        # tendencia e a confirmacao independente da barra gatilho (tamanho, volume, DI+ e RSI nascem nela)
        if not (bool(data.close[-1] > trend_ema) or htf_confluence(context)):
            return None
        checks = {
            "volume_forte": bar.volume_ratio >= cfg.strong_volume_multiple,
            "preco_acima_da_ema_de_tendencia": bool(data.close[-1] > trend_ema),
            "dominio_comprador_di": bool(directional.plus_di[-1] > directional.minus_di[-1]),
            "rsi_em_zona_de_forca": bool(cfg.rsi_min <= rsi_now),
            "fechamento_acima_da_maxima_recente": ElephantContext.STRUCTURE_BREAK in bar.contexts,
            "tendencia_superior_de_alta": htf_confluence(context),
        }
        hits, families, _ = score_confluences(checks)
        if len(families) < cfg.min_confluences:
            return None
        # o volume ja pesa na confianca da barra (bar.confidence): fora da media das familias
        _, _, family_confidence = score_confluences({k: v for k, v in checks.items() if k != "volume_forte"})
        confidence = 0.5 * bar.confidence + 0.5 * family_confidence
        if confidence < cfg.min_confidence:
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
            f"Barra elefante de alta (corpo {bar.body_to_avg_body:.1f}x a media, {bar.body_to_range:.0%} da "
            f"amplitude, volume {bar.volume_ratio:.1f}x) fechou acima da resistencia {resistance:.8g}. "
            f"Confluencias independentes ({len(families)} familias): {', '.join(hits)}."
        )
        return StrategySignal(
            signal=signal,
            confidence=min(1.0, confidence),
            rationale=rationale,
            triggers=TRIGGERS,
            confluences=hits,
        )


def _nearest_resistance(data: CandleArrays, cfg: ElephantBarBreakoutConfig) -> float | None:
    previous_high = data.high[:-1]
    mask = swing_highs(previous_high, cfg.swing_left, cfg.swing_right)
    start = max(0, len(previous_high) - cfg.resistance_lookback)
    levels = previous_high[start:][mask[start:]]
    above = levels[levels >= data.close[-2]]
    return float(np.min(above)) if above.size else None
