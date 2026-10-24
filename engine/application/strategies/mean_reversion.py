from collections.abc import Sequence

from pydantic import Field

from engine.application.indicators import adx, atr, bollinger, macd, rsi, volume_average
from engine.application.strategies._support import CommonConfig, build_signal, prepare, to_decimal
from engine.application.strategies.base import StrategyContext, StrategySignal
from engine.application.strategies.levels import htf_confluence
from engine.domain.candle import Candle


class BollingerRsiConfig(CommonConfig):
    """Reversao a media: perda da banda inferior de Bollinger com RSI sobrevendido, so em mercado lateral.

    Defaults: Bollinger 20 x 2.0 (Bollinger) e RSI <= 30 nas 3 ultimas velas (Wilder); regime lateral =
    ADX < 20 (Wilder), exigido como GATILHO (em tendencia, a banda "caminha" e a reversao falha);
    veto se o alvo liquido de taxas ultrapassa a banda superior (alvo inalcancavel no range) ou se o
    timeframe superior e de baixa (faca caindo).
    """

    bollinger_period: int = Field(default=20, ge=5)
    bollinger_std: float = Field(default=2.0, gt=0)
    adx_period: int = Field(default=14, ge=2)
    adx_max: float = Field(default=20.0, gt=0, le=100)
    rsi_oversold: float = Field(default=30.0, gt=0, lt=50)
    rsi_window: int = Field(default=3, ge=1)
    macd_slow: int = Field(default=26, ge=3)


TRIGGERS = ("regime_lateral_adx_baixo", "retorno_para_dentro_da_banda_inferior", "rsi_sobrevendido")


class BollingerRsiReversion:
    """Fecha de volta para dentro da banda inferior apos perde-la, com RSI sobrevendido e ADX baixo."""

    name = "bollinger_rsi_reversion"

    def __init__(self, config: BollingerRsiConfig | None = None) -> None:
        self.config = config or BollingerRsiConfig()

    def evaluate(self, candles: Sequence[Candle], context: StrategyContext) -> StrategySignal | None:
        cfg = self.config
        needed = (
            max(
                cfg.bollinger_period + 1,
                cfg.adx_period * 2,
                cfg.volume_period + 1,
                cfg.rsi_period + cfg.rsi_window,
                cfg.macd_slow + 10,
            )
            + 2
        )
        data = prepare(candles, needed, cfg.continuity_window)
        if data is None or context.htf_trend == "down":
            return None
        if not adx(data.high, data.low, data.close, cfg.adx_period).adx[-1] < cfg.adx_max:
            return None
        bands = bollinger(data.close, cfg.bollinger_period, cfg.bollinger_std)
        reentered = data.close[-2] < bands.lower[-2] and data.close[-1] > bands.lower[-1]
        rsi_values = rsi(data.close, cfg.rsi_period)
        if not reentered or not min(rsi_values[-cfg.rsi_window :]) <= cfg.rsi_oversold:
            return None

        amplitude = data.high[-1] - data.low[-1]
        histogram = macd(data.close, slow=cfg.macd_slow).histogram
        avg_volume = volume_average(data.volume, cfg.volume_period)[-2]
        checks = {
            "volume_acima_da_media": bool(max(data.volume[-2:]) >= cfg.volume_multiple * avg_volume),
            "candle_de_reversao_de_alta": bool(
                amplitude > 0
                and data.close[-1] > data.open[-1]
                and (data.close[-1] - data.low[-1]) / amplitude >= 0.5
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
            structure_stop=min(candles[-1].low, candles[-2].low),
            checks=checks,
            triggers=TRIGGERS,
            summary="Retorno para dentro da banda inferior com RSI sobrevendido em regime lateral.",
            max_target=to_decimal(bands.upper[-1]),
        )
