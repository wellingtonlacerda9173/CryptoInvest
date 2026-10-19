import math
from collections.abc import Sequence
from enum import StrEnum

from pydantic import Field

from engine.application.indicators import CandleArrays, FloatArray, adx, atr
from engine.domain.base import DomainModel
from engine.domain.candle import Candle
from engine.domain.types import UtcDatetime


class ElephantDirection(StrEnum):
    BULLISH = "bullish"
    BEARISH = "bearish"


class ElephantContext(StrEnum):
    STRUCTURE_BREAK = "structure_break"
    TREND_IGNITION = "trend_ignition"
    EXHAUSTION = "exhaustion"


class ElephantBarConfig(DomainModel):
    lookback: int = Field(default=20, ge=2)
    body_avg_multiple: float = Field(default=1.8, gt=0)
    use_atr_criterion: bool = True
    atr_period: int = Field(default=14, ge=2)
    atr_multiple: float = Field(default=2.0, gt=0)
    min_body_ratio: float = Field(default=0.7, gt=0, le=1)
    volume_multiple: float = Field(default=1.5, gt=0)
    structure_lookback: int = Field(default=20, ge=2)
    adx_period: int = Field(default=14, ge=2)
    ignition_adx_max: float = Field(default=20.0, gt=0, le=100)
    exhaustion_lookback: int = Field(default=10, ge=2)
    exhaustion_atr_multiple: float = Field(default=5.0, gt=0)

    @property
    def min_history(self) -> int:
        return max(self.lookback, self.atr_period, self.structure_lookback, self.exhaustion_lookback + 1) + 1


class ElephantBarResult(DomainModel):
    """Leitura da vela; nunca e uma ordem."""

    index: int
    open_time: UtcDatetime
    direction: ElephantDirection
    contexts: tuple[ElephantContext, ...]
    confidence: float = Field(ge=0, le=1)
    body_to_avg_body: float
    body_to_atr: float | None
    body_to_range: float
    volume_ratio: float
    atr: float | None


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _strength(value: float, threshold: float) -> float:
    # 0,5 exatamente no limiar, 1,0 com o dobro do limiar
    return _clamp(value / (2.0 * threshold))


def detect_elephant_bar(
    candles: Sequence[Candle], config: ElephantBarConfig | None = None, index: int | None = None
) -> ElephantBarResult | None:
    """Avalia a vela em `index` (padrao: a ultima) so com velas ate ela; medias excluem a propria vela."""
    config = config or ElephantBarConfig()
    idx = len(candles) - 1 if index is None else index
    if not 0 <= idx < len(candles):
        raise IndexError("index out of range")
    window = candles[: idx + 1]
    if idx < config.min_history or not all(c.closed for c in window):
        return None

    data = CandleArrays.from_candles(window)
    bar_range = data.high[idx] - data.low[idx]
    if bar_range <= 0:
        return None
    bodies = abs(data.close - data.open)
    body = float(bodies[idx])
    body_ratio = body / bar_range
    avg_body = float(bodies[idx - config.lookback : idx].mean())
    avg_volume = float(data.volume[idx - config.lookback : idx].mean())
    if avg_body <= 0 or avg_volume <= 0:
        return None

    atr_values = atr(data.high, data.low, data.close, config.atr_period)
    prev_atr = float(atr_values[idx - 1])
    atr_ok = config.use_atr_criterion and not math.isnan(prev_atr) and prev_atr > 0
    body_to_avg = body / avg_body
    body_to_atr = body / prev_atr if atr_ok else None
    volume_ratio = float(data.volume[idx]) / avg_volume

    size_ok = body_to_avg >= config.body_avg_multiple or (
        body_to_atr is not None and body_to_atr >= config.atr_multiple
    )
    if not (size_ok and body_ratio >= config.min_body_ratio and volume_ratio >= config.volume_multiple):
        return None

    bullish = data.close[idx] > data.open[idx]
    direction = ElephantDirection.BULLISH if bullish else ElephantDirection.BEARISH
    contexts = _contexts(
        data, idx, config, atr_values, adx(data.high, data.low, data.close, config.adx_period).adx
    )

    size_strength = max(
        _strength(body_to_avg, config.body_avg_multiple),
        _strength(body_to_atr, config.atr_multiple) if body_to_atr is not None else 0.0,
    )
    confidence = (
        size_strength
        + _strength(body_ratio, config.min_body_ratio)
        + _strength(volume_ratio, config.volume_multiple)
    ) / 3.0
    confidence += 0.1 * (ElephantContext.STRUCTURE_BREAK in contexts)
    confidence += 0.05 * (ElephantContext.TREND_IGNITION in contexts)
    confidence -= 0.25 * (ElephantContext.EXHAUSTION in contexts)

    return ElephantBarResult(
        index=idx,
        open_time=window[idx].open_time,
        direction=direction,
        contexts=contexts,
        confidence=_clamp(confidence),
        body_to_avg_body=body_to_avg,
        body_to_atr=body_to_atr,
        body_to_range=body_ratio,
        volume_ratio=volume_ratio,
        atr=prev_atr if atr_ok else None,
    )


def _contexts(
    data: CandleArrays, idx: int, config: ElephantBarConfig, atr_values: FloatArray, adx_values: FloatArray
) -> tuple[ElephantContext, ...]:
    bullish = data.close[idx] > data.open[idx]
    prior = slice(idx - config.structure_lookback, idx)
    broke = data.close[idx] > data.high[prior].max() if bullish else data.close[idx] < data.low[prior].min()
    found: list[ElephantContext] = []
    if broke:
        found.append(ElephantContext.STRUCTURE_BREAK)
    prior_adx = float(adx_values[idx - 1])
    if not math.isnan(prior_adx) and prior_adx < config.ignition_adx_max:
        found.append(ElephantContext.TREND_IGNITION)
    prior_atr = float(atr_values[idx - 1])
    start = idx - 1 - config.exhaustion_lookback
    if start >= 0 and not math.isnan(prior_atr) and prior_atr > 0:
        move = (data.close[idx - 1] - data.close[start]) * (1.0 if bullish else -1.0)
        if move / prior_atr >= config.exhaustion_atr_multiple:
            found.append(ElephantContext.EXHAUSTION)
    return tuple(found)
