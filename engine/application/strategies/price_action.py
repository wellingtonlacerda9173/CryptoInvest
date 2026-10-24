from collections.abc import Sequence

import numpy as np
from pydantic import Field

from engine.application.indicators import (
    CandleArrays,
    FloatArray,
    atr,
    ema,
    rsi,
    swing_highs,
    swing_lows,
    volume_average,
)
from engine.application.strategies._support import CommonConfig, build_signal, prepare, to_decimal
from engine.application.strategies.base import StrategyContext, StrategySignal
from engine.application.strategies.levels import htf_confluence
from engine.domain.candle import Candle


class ReversalConfig(CommonConfig):
    """Padroes de candle de reversao (engolfo e pin bar).

    Defaults: queda previa de 1 ATR em 5 velas (contexto minimo para "reversao"); corpo/amplitude
    minimos de 0.5-0.8 ATR (descarta micro-velas); suporte = pivo de 3+3 velas (convencao do projeto)
    com tolerancia de 0.5 ATR; RSI <= 40 = sobrevenda branda (30 e raro em 15m); volume 1.2x a media.
    """

    volume_multiple: float = Field(default=1.2, gt=0)
    pullback_lookback: int = Field(default=5, ge=2)
    min_decline_atr: float = Field(default=1.0, ge=0)
    min_candle_atr: float = Field(default=0.5, gt=0)
    support_lookback: int = Field(default=50, ge=5)
    swing_left: int = Field(default=3, ge=1)
    swing_right: int = Field(default=3, ge=1)
    support_tolerance_atr: float = Field(default=0.5, gt=0)
    rsi_oversold: float = Field(default=40.0, gt=0, lt=50)


class BullishEngulfingConfig(ReversalConfig):
    pass


class BullishPinBarConfig(ReversalConfig):
    """Martelo: pavio inferior >= 60% da amplitude, pavio superior <= 20%, corpo <= 35% (Nison/Bulkowski)."""

    min_candle_atr: float = Field(default=0.8, gt=0)
    min_lower_wick_ratio: float = Field(default=0.6, gt=0, le=1)
    max_upper_wick_ratio: float = Field(default=0.2, ge=0, lt=1)
    max_body_ratio: float = Field(default=0.35, gt=0, lt=1)


ENGULFING_TRIGGERS = ("engolfo_de_alta", "queda_previa")
PIN_BAR_TRIGGERS = ("pin_bar_de_alta", "queda_previa")


def _reversal_context(
    data: CandleArrays, cfg: ReversalConfig, context: StrategyContext
) -> tuple[float, dict[str, bool]] | None:
    """(ATR, confluencias) se a queda previa existe e nenhum veto vale; None caso contrario."""
    atr_value = float(atr(data.high, data.low, data.close, cfg.atr_period)[-1])
    rsi_values = rsi(data.close, cfg.rsi_period)
    if context.htf_trend == "down" or rsi_values[-1] >= cfg.rsi_veto:
        return None
    base = len(data) - 2 - cfg.pullback_lookback
    if not data.close[base] - data.close[-2] >= cfg.min_decline_atr * atr_value:
        return None
    pattern_low = float(min(data.low[-2:]))
    avg_volume = volume_average(data.volume, cfg.volume_period)[-1]
    checks = {
        "volume_acima_da_media": bool(data.volume[-1] >= cfg.volume_multiple * avg_volume),
        "apoio_em_suporte_confirmado": _near_support(data, cfg, pattern_low, atr_value),
        "rsi_saindo_de_sobrevenda": bool(
            min(rsi_values[-3:-1]) <= cfg.rsi_oversold and rsi_values[-1] > rsi_values[-2]
        ),
        "tendencia_superior_de_alta": htf_confluence(context),
    }
    return atr_value, checks


def _near_support(data: CandleArrays, cfg: ReversalConfig, pattern_low: float, atr_value: float) -> bool:
    previous = data.low[:-1]
    mask = swing_lows(previous, cfg.swing_left, cfg.swing_right)
    start = max(0, len(previous) - cfg.support_lookback)
    levels = previous[start:][mask[start:]]
    return bool(np.any(np.abs(levels - pattern_low) <= cfg.support_tolerance_atr * atr_value))


def _reversal_signal(
    name: str,
    candles: Sequence[Candle],
    data: CandleArrays,
    cfg: ReversalConfig,
    context: StrategyContext,
    triggers: tuple[str, ...],
    summary: str,
) -> StrategySignal | None:
    prepared = _reversal_context(data, cfg, context)
    if prepared is None:
        return None
    atr_value, checks = prepared
    return build_signal(
        name=name,
        candles=candles,
        cfg=cfg,
        context=context,
        atr_value=atr_value,
        structure_stop=min(candles[-1].low, candles[-2].low),
        checks=checks,
        triggers=triggers,
        summary=summary,
    )


def _needed(cfg: ReversalConfig) -> int:
    return (
        max(
            cfg.support_lookback + cfg.swing_right + 1,
            cfg.atr_period + 1,
            cfg.volume_period,
            cfg.pullback_lookback + 2,
            cfg.rsi_period + 3,
        )
        + 2
    )


class BullishEngulfing:
    """Engolfo de alta (corpo da ultima vela cobre o corpo da vela vendedora anterior) apos queda."""

    name = "bullish_engulfing"

    def __init__(self, config: BullishEngulfingConfig | None = None) -> None:
        self.config = config or BullishEngulfingConfig()

    def evaluate(self, candles: Sequence[Candle], context: StrategyContext) -> StrategySignal | None:
        cfg = self.config
        data = prepare(candles, _needed(cfg), cfg.continuity_window)
        if data is None:
            return None
        prev_open, prev_close = data.open[-2], data.close[-2]
        open_, close = data.open[-1], data.close[-1]
        engulfs = (
            prev_close < prev_open
            and close > open_
            and open_ <= prev_close
            and close >= prev_open
            and (open_ < prev_close or close > prev_open)
        )
        atr_value = float(atr(data.high, data.low, data.close, cfg.atr_period)[-1])
        if not engulfs or close - open_ < cfg.min_candle_atr * atr_value:
            return None
        return _reversal_signal(
            self.name,
            candles,
            data,
            cfg,
            context,
            ENGULFING_TRIGGERS,
            f"Engolfo de alta apos queda (corpo de {(close - open_) / atr_value:.1f} ATR).",
        )


class BullishPinBar:
    """Martelo/pin bar de alta: longo pavio inferior rejeitando a minima apos queda."""

    name = "bullish_pin_bar"

    def __init__(self, config: BullishPinBarConfig | None = None) -> None:
        self.config = config or BullishPinBarConfig()

    def evaluate(self, candles: Sequence[Candle], context: StrategyContext) -> StrategySignal | None:
        cfg = self.config
        data = prepare(candles, _needed(cfg), cfg.continuity_window)
        if data is None:
            return None
        high, low, open_, close = data.high[-1], data.low[-1], data.open[-1], data.close[-1]
        amplitude = high - low
        atr_value = float(atr(data.high, data.low, data.close, cfg.atr_period)[-1])
        if amplitude <= 0 or amplitude < cfg.min_candle_atr * atr_value:
            return None
        lower_wick = min(open_, close) - low
        upper_wick = high - max(open_, close)
        is_hammer = (
            lower_wick / amplitude >= cfg.min_lower_wick_ratio
            and upper_wick / amplitude <= cfg.max_upper_wick_ratio
            and abs(close - open_) / amplitude <= cfg.max_body_ratio
        )
        if not is_hammer:
            return None
        return _reversal_signal(
            self.name,
            candles,
            data,
            cfg,
            context,
            PIN_BAR_TRIGGERS,
            f"Martelo apos queda (pavio inferior {lower_wick / amplitude:.0%} da amplitude).",
        )


class SupportRetestConfig(CommonConfig):
    """Reteste de suporte confirmado por pivos.

    Defaults: pivo 3+3 velas; nivel valido com >= 2 toques de pivos a ate 0.3 ATR entre si (um pivo
    isolado e ruido); janela de 100 velas; zona de reteste 0.3 ATR; rejeicao = fechamento na metade
    superior e pavio inferior >= 30% da amplitude; veto se ha resistencia a menos de 1.5 ATR acima.
    """

    swing_left: int = Field(default=3, ge=1)
    swing_right: int = Field(default=3, ge=1)
    level_lookback: int = Field(default=100, ge=10)
    min_touches: int = Field(default=2, ge=2)
    cluster_tolerance_atr: float = Field(default=0.3, gt=0)
    zone_tolerance_atr: float = Field(default=0.3, gt=0)
    min_close_position: float = Field(default=0.5, ge=0, le=1)
    min_lower_wick_ratio: float = Field(default=0.3, ge=0, le=1)
    min_room_atr: float = Field(default=1.5, ge=0)
    rsi_range: tuple[float, float] = (45.0, 65.0)


SUPPORT_TRIGGERS = ("reteste_de_nivel_confirmado", "rejeicao_de_alta_no_nivel")


class SupportRetest:
    """Candle de alta rejeita (pavio) um nivel de suporte/ex-resistencia confirmado por pivos."""

    name = "support_retest"

    def __init__(self, config: SupportRetestConfig | None = None) -> None:
        self.config = config or SupportRetestConfig()

    def evaluate(self, candles: Sequence[Candle], context: StrategyContext) -> StrategySignal | None:
        cfg = self.config
        needed = max(cfg.level_lookback + cfg.swing_right + 1, cfg.trend_ema_period, cfg.atr_period + 1) + 2
        data = prepare(candles, needed, cfg.continuity_window)
        if data is None or context.htf_trend == "down":
            return None
        last = candles[-1]
        atr_value = float(atr(data.high, data.low, data.close, cfg.atr_period)[-1])
        rsi_values = rsi(data.close, cfg.rsi_period)
        if rsi_values[-1] >= cfg.rsi_veto:
            return None

        pivot_lows, pivot_highs = _pivots(data, cfg)
        level = self._retested_level(data, np.concatenate([pivot_lows, pivot_highs]), atr_value)
        if level is None:
            return None
        resistances = pivot_highs[pivot_highs > data.close[-1]]
        if resistances.size and resistances.min() - data.close[-1] < cfg.min_room_atr * atr_value:
            return None

        avg_volume = volume_average(data.volume, cfg.volume_period)[-1]
        low_rsi, high_rsi = cfg.rsi_range
        checks = {
            "volume_acima_da_media": bool(data.volume[-1] >= cfg.volume_multiple * avg_volume),
            "preco_acima_da_ema_de_tendencia": bool(
                data.close[-1] > ema(data.close, cfg.trend_ema_period)[-1]
            ),
            "rsi_com_espaco": bool(low_rsi <= rsi_values[-1] <= high_rsi and rsi_values[-1] > rsi_values[-2]),
            "estrutura_de_alta_preservada": bool(len(pivot_lows) >= 2 and pivot_lows[-1] > pivot_lows[-2]),
            "tendencia_superior_de_alta": htf_confluence(context),
        }
        return build_signal(
            name=self.name,
            candles=candles,
            cfg=cfg,
            context=context,
            atr_value=atr_value,
            structure_stop=min(last.low, to_decimal(level)),
            checks=checks,
            triggers=SUPPORT_TRIGGERS,
            summary=f"Reteste com rejeicao do nivel {level:.8g}, confirmado por pivos.",
        )

    def _retested_level(self, data: CandleArrays, pivots: FloatArray, atr_value: float) -> float | None:
        cfg = self.config
        zone = cfg.zone_tolerance_atr * atr_value
        amplitude = data.high[-1] - data.low[-1]
        if amplitude <= 0 or not data.close[-1] > data.open[-1]:
            return None
        if (data.close[-1] - data.low[-1]) / amplitude < cfg.min_close_position:
            return None
        if (min(data.open[-1], data.close[-1]) - data.low[-1]) / amplitude < cfg.min_lower_wick_ratio:
            return None
        candidates = [
            float(p)
            for p in pivots
            if abs(data.low[-1] - p) <= zone
            and data.close[-1] > p
            and data.close[-2] >= p - zone
            and int(np.sum(np.abs(pivots - p) <= cfg.cluster_tolerance_atr * atr_value)) >= cfg.min_touches
        ]
        return min(candidates, key=lambda p: abs(data.low[-1] - p)) if candidates else None


def _pivots(data: CandleArrays, cfg: SupportRetestConfig) -> tuple[FloatArray, FloatArray]:
    """Pivos confirmados (ordem cronologica) das velas ANTERIORES a ultima, dentro da janela."""
    lows, highs = data.low[:-1], data.high[:-1]
    start = max(0, len(lows) - cfg.level_lookback)
    low_mask = swing_lows(lows, cfg.swing_left, cfg.swing_right)
    high_mask = swing_highs(highs, cfg.swing_left, cfg.swing_right)
    return lows[start:][low_mask[start:]], highs[start:][high_mask[start:]]
