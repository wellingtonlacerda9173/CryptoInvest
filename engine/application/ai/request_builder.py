"""Monta o `AnalystRequest` a partir de velas e do sinal: so mercado, nada de conta ou identidade."""

from collections.abc import Sequence

import numpy as np

from engine.application.ai.models import (
    MAX_CANDLES,
    MAX_ITEMS,
    AnalystRequest,
    CandleView,
    IndicatorsView,
    ScoreView,
    SignalView,
)
from engine.application.indicators import CandleArrays, FloatArray, adx, atr, ema, rsi, volume_average
from engine.application.regime import RegimeConfig, classify_regime
from engine.application.strategies import StrategySignal
from engine.domain.candle import Candle


def _last(values: FloatArray) -> float | None:
    value = float(values[-1]) if len(values) else float("nan")
    return round(value, 4) if np.isfinite(value) else None


def _indicators(window: Sequence[Candle]) -> IndicatorsView:
    arrays = CandleArrays.from_candles(window)
    close = arrays.close
    atr_value = _last(atr(arrays.high, arrays.low, close))
    average_volume = _last(volume_average(arrays.volume, 20))
    fast, slow = _last(ema(close, 20)), _last(ema(close, 50))
    return IndicatorsView(
        rsi14=_last(rsi(close)),
        adx14=_last(adx(arrays.high, arrays.low, close).adx),
        atr14_pct=None if atr_value is None else round(100 * atr_value / float(close[-1]), 4),
        volume_ratio=(
            None
            if not average_volume or average_volume <= 0
            else round(float(arrays.volume[-1]) / average_volume, 4)
        ),
        ema20_vs_ema50_pct=None if fast is None or not slow else round(100 * (fast - slow) / slow, 4),
    )


def build_request(
    window: Sequence[Candle],
    result: StrategySignal,
    *,
    scores: Sequence[tuple[str, float]] = (),
    regime: str | None = None,
    lessons: Sequence[str] = (),
) -> AnalystRequest:
    signal = result.signal
    recent = list(window)[-MAX_CANDLES:]
    if regime is None:
        try:
            regime = classify_regime(list(window), RegimeConfig()).regime.label
        except ValueError:
            regime = "indefinido"
    return AnalystRequest(
        symbol=signal.symbol,
        timeframe=signal.timeframe.value,
        regime=regime,
        candles=tuple(
            CandleView(
                ago=len(recent) - 1 - i,
                open=float(c.open),
                high=float(c.high),
                low=float(c.low),
                close=float(c.close),
                volume=float(c.volume),
            )
            for i, c in enumerate(recent)
        ),
        indicators=_indicators(window),
        signal=SignalView(
            strategy=signal.strategy,
            entry=float(signal.entry),
            stop=float(signal.stop_loss),
            target=float(signal.take_profit),
            reward_risk=round(float(signal.reward_risk), 4),
            confidence=result.confidence,
            confluences=tuple(result.confluences[:MAX_ITEMS]),
        ),
        selector_scores=tuple(ScoreView(strategy=n, score=s) for n, s in scores[:MAX_ITEMS]),
        lessons=tuple(lessons),
    )
