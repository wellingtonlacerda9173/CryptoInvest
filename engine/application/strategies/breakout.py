from collections.abc import Sequence

import numpy as np
from pydantic import Field

from engine.application.indicators import adx, atr, donchian, ema, rsi, swing_highs, volume_average
from engine.application.strategies._support import CommonConfig, build_signal, prepare, to_decimal
from engine.application.strategies.base import StrategyContext, StrategySignal
from engine.application.strategies.levels import LevelsConfig, StopMode, htf_confluence
from engine.domain.candle import Candle


class DonchianBreakoutConfig(CommonConfig):
    """Rompimento do canal de Donchian.

    Defaults: canal de 20 velas (Turtle System 1); stop de 2 ATR (o stop turtle de 2N); volume 1.5x a
    media como confirmacao; veto de perseguicao: fechar a mais de 2 ATR acima do canal; RSI 50-70.
    """

    levels: LevelsConfig = LevelsConfig(stop_mode=StopMode.ATR, atr_stop_multiple=2.0)
    volume_multiple: float = Field(default=1.5, gt=0)
    channel_period: int = Field(default=20, ge=5)
    max_extension_atr: float = Field(default=2.0, gt=0)
    clean_extension_atr: float = Field(default=1.0, gt=0)
    max_candle_atr: float = Field(default=2.5, gt=0)
    rsi_range: tuple[float, float] = (50.0, 70.0)


DONCHIAN_TRIGGERS = ("rompimento_do_canal_de_donchian",)


class DonchianBreakout:
    """Fechamento acima da maxima das `channel_period` velas anteriores."""

    name = "donchian_breakout"

    def __init__(self, config: DonchianBreakoutConfig | None = None) -> None:
        self.config = config or DonchianBreakoutConfig()

    def evaluate(self, candles: Sequence[Candle], context: StrategyContext) -> StrategySignal | None:
        cfg = self.config
        needed = (
            max(
                cfg.channel_period + 1,
                cfg.trend_ema_period,
                cfg.atr_period + 1,
                cfg.volume_period,
                cfg.rsi_period + 1,
            )
            + 2
        )
        data = prepare(candles, needed, cfg.continuity_window)
        if data is None or context.htf_trend == "down":
            return None
        # canal ate a vela anterior: a vela do rompimento nao participa do proprio nivel
        upper = float(donchian(data.high, data.low, cfg.channel_period).upper[-2])
        atr_value = float(atr(data.high, data.low, data.close, cfg.atr_period)[-1])
        extension = data.close[-1] - upper
        rsi_now = float(rsi(data.close, cfg.rsi_period)[-1])
        if extension <= 0 or extension > cfg.max_extension_atr * atr_value or rsi_now >= cfg.rsi_veto:
            return None

        low_rsi, high_rsi = cfg.rsi_range
        checks = {
            "volume_acima_da_media": bool(
                data.volume[-1] >= cfg.volume_multiple * volume_average(data.volume, cfg.volume_period)[-1]
            ),
            "preco_acima_da_ema_de_tendencia": bool(
                data.close[-1] > ema(data.close, cfg.trend_ema_period)[-1]
            ),
            "rsi_em_zona_de_forca": low_rsi <= rsi_now <= high_rsi,
            "rompimento_sem_exaustao": bool(
                extension <= cfg.clean_extension_atr * atr_value
                and data.high[-1] - data.low[-1] <= cfg.max_candle_atr * atr_value
            ),
            "tendencia_superior_de_alta": htf_confluence(context),
        }
        return build_signal(
            name=self.name,
            candles=candles,
            cfg=cfg,
            context=context,
            atr_value=atr_value,
            structure_stop=candles[-1].low,
            checks=checks,
            triggers=DONCHIAN_TRIGGERS,
            summary=f"Fechamento acima do canal de Donchian({cfg.channel_period}) em {upper:.8g}.",
        )


class RangeBreakoutConfig(CommonConfig):
    """Rompimento de range lateral com volume.

    Defaults: range das 30 velas anteriores; lateral = largura <= 6 ATR e ADX < 25 (Wilder: abaixo de
    20-25 nao ha tendencia); volume do rompimento >= 1.5x a media (GATILHO, nao conta como confluencia:
    por isso o setup exige tendencia, momentum e estrutura); resistencia testada por >= 2 pivos a
    0.25 ATR do topo; veto se fecha a mais de 2 ATR acima do range.
    """

    range_period: int = Field(default=30, ge=10)
    max_width_atr: float = Field(default=6.0, gt=0)
    adx_period: int = Field(default=14, ge=2)
    adx_lateral_max: float = Field(default=25.0, gt=0, le=100)
    breakout_volume_multiple: float = Field(default=1.5, gt=0)
    swing_left: int = Field(default=3, ge=1)
    swing_right: int = Field(default=3, ge=1)
    min_touches: int = Field(default=2, ge=2)
    touch_tolerance_atr: float = Field(default=0.25, gt=0)
    max_extension_atr: float = Field(default=2.0, gt=0)
    rsi_range: tuple[float, float] = (50.0, 70.0)


RANGE_TRIGGERS = ("rompimento_de_range_lateral", "range_lateral_valido", "volume_no_rompimento")


class RangeBreakout:
    """Fechamento acima de um range lateral valido, com volume anormal."""

    name = "range_breakout"

    def __init__(self, config: RangeBreakoutConfig | None = None) -> None:
        self.config = config or RangeBreakoutConfig()

    def evaluate(self, candles: Sequence[Candle], context: StrategyContext) -> StrategySignal | None:
        cfg = self.config
        needed = (
            max(
                cfg.range_period + 1,
                cfg.trend_ema_period,
                cfg.adx_period * 2,
                cfg.volume_period,
                cfg.rsi_period + 1,
            )
            + 2
        )
        data = prepare(candles, needed, cfg.continuity_window)
        if data is None or context.htf_trend == "down":
            return None
        window_high = data.high[-cfg.range_period - 1 : -1]
        range_high = float(window_high.max())
        width = range_high - float(data.low[-cfg.range_period - 1 : -1].min())
        atr_value = float(atr(data.high, data.low, data.close, cfg.atr_period)[-1])
        extension = data.close[-1] - range_high
        rsi_now = float(rsi(data.close, cfg.rsi_period)[-1])
        if extension <= 0 or extension > cfg.max_extension_atr * atr_value or rsi_now >= cfg.rsi_veto:
            return None
        avg_volume = volume_average(data.volume, cfg.volume_period)[-2]
        lateral = (
            width <= cfg.max_width_atr * atr_value
            and adx(data.high, data.low, data.close, cfg.adx_period).adx[-2] < cfg.adx_lateral_max
        )
        if not lateral or data.volume[-1] < cfg.breakout_volume_multiple * avg_volume:
            return None

        pivots = window_high[swing_highs(window_high, cfg.swing_left, cfg.swing_right)]
        touches = int(np.sum(pivots >= range_high - cfg.touch_tolerance_atr * atr_value))
        low_rsi, high_rsi = cfg.rsi_range
        checks = {
            "preco_acima_da_ema_de_tendencia": bool(
                data.close[-1] > ema(data.close, cfg.trend_ema_period)[-1]
            ),
            "tendencia_superior_de_alta": htf_confluence(context),
            "rsi_em_zona_de_forca": low_rsi <= rsi_now <= high_rsi,
            "range_testado_na_resistencia": touches >= cfg.min_touches,
        }
        return build_signal(
            name=self.name,
            candles=candles,
            cfg=cfg,
            context=context,
            atr_value=atr_value,
            structure_stop=min(candles[-1].low, to_decimal(range_high)),
            checks=checks,
            triggers=RANGE_TRIGGERS,
            summary=f"Rompimento do range lateral de {cfg.range_period} velas (topo {range_high:.8g}) "
            f"com volume {data.volume[-1] / avg_volume:.1f}x a media.",
        )
