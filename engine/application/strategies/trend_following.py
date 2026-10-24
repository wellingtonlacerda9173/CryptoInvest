from collections.abc import Sequence

from pydantic import Field

from engine.application.indicators import adx, atr, ema, rsi, supertrend, volume_average
from engine.application.strategies._support import CommonConfig, build_signal, prepare, to_decimal
from engine.application.strategies.base import StrategyContext, StrategySignal
from engine.application.strategies.levels import htf_confluence
from engine.domain.candle import Candle


class SupertrendFlipConfig(CommonConfig):
    """Supertrend virando para alta.

    Defaults: ATR 10 x 3.0 (parametros originais de Olivier Seban); ADX >= 15 como veto de mercado
    lateral (abaixo disso o Supertrend gera whipsaw; 15 e mais frouxo que 20 porque o ADX ainda sobe
    na virada); stop estrutural na propria linha do Supertrend; RSI 50-70 = forca sem exaustao.
    """

    supertrend_period: int = Field(default=10, ge=2)
    supertrend_multiple: float = Field(default=3.0, gt=0)
    supertrend_warmup: int = Field(default=30, ge=0)
    adx_period: int = Field(default=14, ge=2)
    adx_min: float = Field(default=15.0, gt=0, le=100)
    structure_lookback: int = Field(default=10, ge=2)
    rsi_range: tuple[float, float] = (50.0, 70.0)


TRIGGERS = ("supertrend_virou_para_alta",)


class SupertrendFlip:
    """Supertrend passa de baixa para alta na ultima vela fechada, fora de mercado lateral."""

    name = "supertrend_flip"

    def __init__(self, config: SupertrendFlipConfig | None = None) -> None:
        self.config = config or SupertrendFlipConfig()

    def evaluate(self, candles: Sequence[Candle], context: StrategyContext) -> StrategySignal | None:
        cfg = self.config
        needed = (
            max(
                cfg.supertrend_period + cfg.supertrend_warmup,
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
        trend = supertrend(data.high, data.low, data.close, cfg.supertrend_period, cfg.supertrend_multiple)
        if not (trend.direction[-2] < 0 < trend.direction[-1]):
            return None
        directional = adx(data.high, data.low, data.close, cfg.adx_period)
        rsi_now = float(rsi(data.close, cfg.rsi_period)[-1])
        if directional.adx[-1] < cfg.adx_min or rsi_now >= cfg.rsi_veto:
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
            "fechamento_acima_da_maxima_recente": bool(
                data.close[-1] > data.high[-cfg.structure_lookback - 1 : -1].max()
            ),
            "tendencia_superior_de_alta": htf_confluence(context),
        }
        return build_signal(
            name=self.name,
            candles=candles,
            cfg=cfg,
            context=context,
            atr_value=float(atr(data.high, data.low, data.close, cfg.atr_period)[-1]),
            structure_stop=to_decimal(trend.line[-1]),
            checks=checks,
            triggers=TRIGGERS,
            summary=f"Supertrend({cfg.supertrend_period},{cfg.supertrend_multiple:g}) virou para alta "
            f"com ADX {directional.adx[-1]:.1f}.",
        )
