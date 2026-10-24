from collections.abc import Sequence
from datetime import timedelta

import numpy as np
from pydantic import Field

from engine.application.indicators import atr, ema, rsi, session_vwap, volume_average
from engine.application.strategies._support import CommonConfig, build_signal, prepare
from engine.application.strategies.base import StrategyContext, StrategySignal
from engine.application.strategies.levels import htf_confluence
from engine.domain.candle import Candle
from engine.domain.types import Timeframe


class VwapConfig(CommonConfig):
    """Reclaim/rejeicao do VWAP da sessao UTC (reinicia a 00:00 UTC).

    Defaults: sessao com >= 8 velas completas (VWAP das primeiras velas e so o preco); toque do VWAP
    tolerado ate 0.3 ATR; veto de perseguicao: fechar a mais de 1 ATR acima do VWAP; RSI 50-70;
    stop estrutural na menor minima das 3 ultimas velas; timeframe diario ou maior nao tem sessao.
    """

    min_session_candles: int = Field(default=8, ge=2)
    touch_tolerance_atr: float = Field(default=0.3, ge=0)
    max_extension_atr: float = Field(default=1.0, gt=0)
    structure_lookback: int = Field(default=5, ge=2)
    rsi_range: tuple[float, float] = (50.0, 70.0)


class VwapReclaim:
    """Fechamento recupera o VWAP por baixo (reclaim) ou o testa por cima e rejeita (bounce)."""

    name = "vwap_reclaim"

    def __init__(self, config: VwapConfig | None = None) -> None:
        self.config = config or VwapConfig()

    def evaluate(self, candles: Sequence[Candle], context: StrategyContext) -> StrategySignal | None:
        cfg = self.config
        needed = max(cfg.trend_ema_period, cfg.atr_period + 1, cfg.volume_period, cfg.rsi_period + 1) + 2
        if candles and candles[-1].timeframe is Timeframe.D1:
            return None
        data = prepare(candles, needed, cfg.continuity_window)
        if data is None or context.htf_trend == "down":
            return None
        if not self._session_is_complete(candles):
            return None

        session = np.array([c.open_time.date().toordinal() for c in candles], dtype=np.int64)
        vwap = session_vwap(data.high, data.low, data.close, data.volume, session)
        atr_value = float(atr(data.high, data.low, data.close, cfg.atr_period)[-1])
        if session[-2] != session[-1] or np.isnan(vwap[-2:]).any():
            return None
        tolerance = cfg.touch_tolerance_atr * atr_value
        above = data.close[-1] > vwap[-1]
        reclaimed = data.close[-2] < vwap[-2] and above
        bounced = (
            data.close[-2] > vwap[-2]
            and above
            and data.close[-1] > data.open[-1]
            and vwap[-1] - tolerance <= data.low[-1] <= vwap[-1] + tolerance
        )
        rsi_now = float(rsi(data.close, cfg.rsi_period)[-1])
        if not (reclaimed or bounced) or rsi_now >= cfg.rsi_veto:
            return None
        if data.close[-1] - vwap[-1] > cfg.max_extension_atr * atr_value:
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
        trigger = "reclaim_do_vwap" if reclaimed else "rejeicao_no_vwap"
        return build_signal(
            name=self.name,
            candles=candles,
            cfg=cfg,
            context=context,
            atr_value=atr_value,
            structure_stop=min(c.low for c in candles[-3:]),
            checks=checks,
            triggers=(trigger, "sessao_com_amostra_minima"),
            summary=f"{'Reclaim' if reclaimed else 'Rejeicao'} do VWAP da sessao ({vwap[-1]:.8g}).",
        )

    def supports(self, timeframe: Timeframe) -> bool:
        """Sessao UTC de um dia com menos velas que o minimo (ex.: 6 velas de 4h < 8) nunca gera sinal."""
        return 86_400 // timeframe.seconds >= self.config.min_session_candles

    def _session_is_complete(self, candles: Sequence[Candle]) -> bool:
        """Sessao atual com amostra minima E presente desde 00:00 UTC (senao o VWAP seria parcial)."""
        last = candles[-1]
        midnight = last.open_time.replace(hour=0, minute=0, second=0, microsecond=0)
        expected = int((last.open_time - midnight) / timedelta(seconds=last.timeframe.seconds)) + 1
        in_session = sum(1 for c in candles if c.open_time >= midnight)
        return in_session == expected and expected >= self.config.min_session_candles
