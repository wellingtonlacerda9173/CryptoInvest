from collections.abc import Sequence
from typing import Self

import numpy as np
from pydantic import Field, model_validator

from engine.application.indicators import adx, atr, ema, macd, rsi, swing_lows, volume_average
from engine.application.strategies._support import CommonConfig, build_signal, prepare
from engine.application.strategies.base import StrategyContext, StrategySignal
from engine.application.strategies.levels import LevelsConfig, StopMode, htf_confluence
from engine.domain.candle import Candle


class MacdCrossConfig(CommonConfig):
    """Cruzamento do MACD sobre a linha de sinal.

    Defaults: MACD 12/26/9 (Appel); ADX >= 20 como gatilho (o MACD gera whipsaw sem tendencia); stop de
    1.5 ATR; confirmacoes independentes: volume, tendencia, RSI 50-70, fechamento acima da maxima de 10 velas.
    """

    levels: LevelsConfig = LevelsConfig(stop_mode=StopMode.ATR)
    macd_fast: int = Field(default=12, ge=2)
    macd_slow: int = Field(default=26, ge=3)
    macd_signal: int = Field(default=9, ge=2)
    adx_period: int = Field(default=14, ge=2)
    adx_min: float = Field(default=20.0, gt=0, le=100)
    structure_lookback: int = Field(default=10, ge=2)
    rsi_range: tuple[float, float] = (50.0, 70.0)

    @model_validator(mode="after")
    def _check_periods(self) -> Self:
        if not self.macd_fast < self.macd_slow:
            raise ValueError("macd_fast must be < macd_slow")
        return self


MACD_TRIGGERS = ("cruzamento_macd_para_cima", "adx_em_tendencia")


class MacdCross:
    """Linha MACD cruza acima da linha de sinal na ultima vela fechada, com ADX em tendencia."""

    name = "macd_cross"

    def __init__(self, config: MacdCrossConfig | None = None) -> None:
        self.config = config or MacdCrossConfig()

    def evaluate(self, candles: Sequence[Candle], context: StrategyContext) -> StrategySignal | None:
        cfg = self.config
        needed = (
            max(
                cfg.macd_slow + cfg.macd_signal + 5,
                cfg.trend_ema_period,
                cfg.adx_period * 2,
                cfg.volume_period,
                cfg.rsi_period + 1,
                cfg.structure_lookback + 1,
            )
            + 2
        )
        data = prepare(candles, needed, cfg.continuity_window)
        if data is None or context.htf_trend == "down":
            return None
        lines = macd(data.close, cfg.macd_fast, cfg.macd_slow, cfg.macd_signal)
        crossed_up = lines.macd[-2] <= lines.signal[-2] and lines.macd[-1] > lines.signal[-1]
        directional = adx(data.high, data.low, data.close, cfg.adx_period)
        rsi_now = float(rsi(data.close, cfg.rsi_period)[-1])
        if not crossed_up or directional.adx[-1] < cfg.adx_min or rsi_now >= cfg.rsi_veto:
            return None

        low_rsi, high_rsi = cfg.rsi_range
        checks = {
            "volume_acima_da_media": bool(
                data.volume[-1] >= cfg.volume_multiple * volume_average(data.volume, cfg.volume_period)[-1]
            ),
            "preco_acima_da_ema_de_tendencia": bool(
                data.close[-1] > ema(data.close, cfg.trend_ema_period)[-1]
            ),
            "tendencia_superior_de_alta": htf_confluence(context),
            "rsi_em_zona_de_forca": low_rsi <= rsi_now <= high_rsi,
            "fechamento_acima_da_maxima_recente": bool(
                data.close[-1] > data.high[-cfg.structure_lookback - 1 : -1].max()
            ),
        }
        return build_signal(
            name=self.name,
            candles=candles,
            cfg=cfg,
            context=context,
            atr_value=float(atr(data.high, data.low, data.close, cfg.atr_period)[-1]),
            structure_stop=candles[-1].low,
            checks=checks,
            triggers=MACD_TRIGGERS,
            summary=f"MACD cruzou acima do sinal com ADX {directional.adx[-1]:.1f} (minimo {cfg.adx_min:g}).",
        )


class RsiDivergenceConfig(CommonConfig):
    """Divergencia de alta do RSI: minima mais baixa no preco, minima mais alta no RSI.

    Defaults: pivos 3+3 (a divergencia so existe quando o 2o pivo fecha a confirmacao, exatamente na
    ultima vela: sem lookahead); pivos separados por 5-40 velas; RSI do 1o pivo <= 40 (divergencia em
    zona fraca) e melhora minima de 2 pontos de RSI; confirmacao = fechamento acima da maxima da vela
    anterior; rejeicao no pivo = pavio inferior >= 40% da amplitude.
    """

    volume_multiple: float = Field(default=1.2, gt=0)
    swing_left: int = Field(default=3, ge=1)
    swing_right: int = Field(default=3, ge=1)
    min_pivot_gap: int = Field(default=5, ge=2)
    max_pivot_gap: int = Field(default=40, ge=5)
    first_pivot_rsi_max: float = Field(default=40.0, gt=0, lt=50)
    min_rsi_improvement: float = Field(default=2.0, gt=0)
    min_pivot_wick_ratio: float = Field(default=0.4, ge=0, le=1)
    macd_slow: int = Field(default=26, ge=3)

    @model_validator(mode="after")
    def _check_gap(self) -> Self:
        if not self.min_pivot_gap < self.max_pivot_gap:
            raise ValueError("min_pivot_gap must be < max_pivot_gap")
        return self


RSI_DIVERGENCE_TRIGGERS = ("divergencia_de_alta_do_rsi", "confirmacao_do_pivo_a_direita")


class RsiBullishDivergence:
    """Divergencia de alta entre dois pivos de fundo, sinalizada na vela que confirma o segundo pivo."""

    name = "rsi_bullish_divergence"

    def __init__(self, config: RsiDivergenceConfig | None = None) -> None:
        self.config = config or RsiDivergenceConfig()

    def evaluate(self, candles: Sequence[Candle], context: StrategyContext) -> StrategySignal | None:
        cfg = self.config
        needed = (
            max(
                cfg.max_pivot_gap + cfg.swing_left + cfg.swing_right + 1,
                cfg.rsi_period + 1,
                cfg.atr_period + 1,
                cfg.volume_period,
                cfg.macd_slow + 10,
            )
            + 2
        )
        data = prepare(candles, needed, cfg.continuity_window)
        if data is None or context.htf_trend == "down":
            return None
        rsi_values = rsi(data.close, cfg.rsi_period)
        if rsi_values[-1] >= cfg.rsi_veto or not data.close[-1] > data.high[-2]:
            return None
        second = len(data) - 1 - cfg.swing_right
        pivot_mask = swing_lows(data.low, cfg.swing_left, cfg.swing_right)
        if not pivot_mask[second]:
            return None
        window = np.flatnonzero(pivot_mask[: second - cfg.min_pivot_gap + 1])
        window = window[window >= second - cfg.max_pivot_gap]
        if window.size == 0:
            return None
        first = int(window[-1])
        diverges = (
            data.low[second] < data.low[first]
            and rsi_values[first] <= cfg.first_pivot_rsi_max
            and rsi_values[second] - rsi_values[first] >= cfg.min_rsi_improvement
        )
        if not diverges:
            return None

        pivot_range = data.high[second] - data.low[second]
        pivot_wick = min(data.open[second], data.close[second]) - data.low[second]
        histogram = macd(data.close, slow=cfg.macd_slow).histogram
        checks = {
            "volume_acima_da_media": bool(
                data.volume[-1] >= cfg.volume_multiple * volume_average(data.volume, cfg.volume_period)[-1]
            ),
            "candle_de_reversao_de_alta": bool(
                pivot_range > 0 and pivot_wick / pivot_range >= cfg.min_pivot_wick_ratio
            ),
            "histograma_macd_subindo": bool(histogram[-1] > histogram[-2]),
            "tendencia_superior_de_alta": htf_confluence(context),
        }
        return build_signal(
            name=self.name,
            candles=candles,
            cfg=cfg,
            context=context,
            atr_value=float(atr(data.high, data.low, data.close, cfg.atr_period)[-1]),
            structure_stop=candles[second].low,
            checks=checks,
            triggers=RSI_DIVERGENCE_TRIGGERS,
            summary=f"Divergencia de alta: preco fez minima mais baixa ({data.low[second]:.8g}) e o RSI "
            f"subiu de {rsi_values[first]:.1f} para {rsi_values[second]:.1f}.",
        )
