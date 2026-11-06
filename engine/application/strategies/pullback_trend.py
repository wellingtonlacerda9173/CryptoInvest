import math
from collections.abc import Sequence
from decimal import Decimal
from typing import Self

from pydantic import Field, model_validator

from engine.application.indicators import CandleArrays, atr, ema
from engine.application.strategies.base import MIN_CONFLUENCES, StrategyContext, StrategySignal
from engine.application.strategies.confluence import score_confluences
from engine.application.strategies.continuity import candles_are_continuous
from engine.application.strategies.levels import (
    LevelsConfig,
    StopMode,
    compute_levels,
    effective_fee_rate,
)
from engine.domain.base import DomainModel
from engine.domain.candle import Candle
from engine.domain.signal import Signal


class PullbackTrendConfig(DomainModel):
    """Padroes = script Pine "Pullback na Tendencia" (compra). Nao otimizados em dados.

    `levels.min_risk_atr` e a distancia minima do stop (x ATR); o stop estrutural vem SEM folga extra
    (`stop_buffer_atr=0`), como no script. `levels.max_risk_atr` (teto do projeto) descarta stops largos
    demais, algo que o script original nao faz. `levels.reward_risk` e o alvo em R LIQUIDO de taxas.
    """

    levels: LevelsConfig = LevelsConfig(stop_mode=StopMode.STRUCTURE, stop_buffer_atr=0.0, min_risk_atr=0.5)
    trend_period: int = Field(default=200, ge=10)
    pullback_period: int = Field(default=20, ge=5)
    pullback_bars: int = Field(default=5, ge=1)
    slope_bars: int = Field(default=10, ge=1)
    swing_bars: int = Field(default=10, ge=2)
    atr_period: int = Field(default=14, ge=2)
    continuity_window: int = Field(default=100, ge=2)
    min_confluences: int = Field(default=MIN_CONFLUENCES, ge=MIN_CONFLUENCES)
    min_confidence: float = Field(default=0.5, ge=0, le=1)

    @model_validator(mode="after")
    def _check_periods(self) -> Self:
        if not self.pullback_period < self.trend_period:
            raise ValueError("pullback_period must be < trend_period")
        return self


TRIGGERS = ("pullback_com_retomada_na_tendencia",)

# As tres regras obrigatorias do script, uma por familia independente (tendencia, estrutura, momentum).
# Nao ha filtro extra: a contagem de familias e satisfeita por construcao e a confianca e sempre 1.0.
CHECKS = {
    "tendencia_de_alta_ema_longa": True,
    "pullback_na_ema_curta": True,
    "retomada_acima_da_maxima_anterior": True,
}


def is_uptrend(close: float, fast_ema: float, trend_ema: float, trend_ema_before: float) -> bool:
    """Fechamento e media curta acima da media longa, e a longa subindo (vs. `slope_bars` velas atras)."""
    return close > trend_ema and fast_ema > trend_ema and trend_ema > trend_ema_before


class PullbackTrend:
    """Compra a retomada de um pullback na EMA curta dentro de tendencia de alta (EMA longa)."""

    name = "pullback_trend"

    def __init__(self, config: PullbackTrendConfig | None = None) -> None:
        self.config = config or PullbackTrendConfig()

    def evaluate(self, candles: Sequence[Candle], context: StrategyContext) -> StrategySignal | None:
        cfg = self.config
        needed = (
            max(
                cfg.trend_period + cfg.slope_bars,
                cfg.pullback_period + cfg.pullback_bars,
                cfg.swing_bars,
                cfg.atr_period + 1,
            )
            + 1
        )
        if len(candles) < needed:
            return None
        last = candles[-1]
        if not all(c.closed for c in candles) or not candles_are_continuous(
            candles, last.timeframe, max(cfg.continuity_window, needed)
        ):
            return None

        data = CandleArrays.from_candles(candles)
        trend = ema(data.close, cfg.trend_period)
        fast = ema(data.close, cfg.pullback_period)
        atr_value = float(atr(data.high, data.low, data.close, cfg.atr_period)[-1])
        values = (trend[-1], trend[-1 - cfg.slope_bars], fast[-1], atr_value)
        if not all(math.isfinite(float(v)) for v in values) or atr_value <= 0:
            return None

        in_uptrend = is_uptrend(
            float(data.close[-1]), float(fast[-1]), float(trend[-1]), float(trend[-1 - cfg.slope_bars])
        )
        window = slice(-cfg.pullback_bars, None)
        touched = bool((data.low[window] <= fast[window]).any())
        resumed = bool(data.close[-1] > fast[-1] and data.close[-1] > data.high[-2])
        if not (in_uptrend and touched and resumed):
            return None

        swing_low = min(c.low for c in candles[-cfg.swing_bars :])
        min_distance = Decimal(repr(float(cfg.levels.min_risk_atr * atr_value)))
        levels = compute_levels(
            last.close,
            atr_value,
            min(swing_low, last.close - min_distance),
            cfg.levels,
            effective_fee_rate(cfg.levels, context),
        )
        if levels is None:
            return None
        stop, target = levels

        hits, families, confidence = score_confluences(CHECKS)
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
            f"Tendencia de alta (fechamento e EMA{cfg.pullback_period} acima da EMA{cfg.trend_period}, "
            f"que sobe ha {cfg.slope_bars} velas); a minima tocou a EMA{cfg.pullback_period} nas ultimas "
            f"{cfg.pullback_bars} velas e o fechamento retomou acima dela e da maxima anterior. "
            f"Familias confirmadas ({len(families)}): {', '.join(hits)}."
        )
        return StrategySignal(
            signal=signal, confidence=confidence, rationale=rationale, triggers=TRIGGERS, confluences=hits
        )
