from collections.abc import Mapping, Sequence
from decimal import Decimal

from pydantic import Field

from engine.application.indicators import CandleArrays
from engine.application.strategies.base import MIN_CONFLUENCES, StrategyContext, StrategySignal
from engine.application.strategies.confluence import score_confluences
from engine.application.strategies.continuity import candles_are_continuous
from engine.application.strategies.levels import LevelsConfig, compute_levels, effective_fee_rate
from engine.domain.base import DomainModel
from engine.domain.candle import Candle
from engine.domain.signal import Signal


class CommonConfig(DomainModel):
    """Parametros comuns, pre-fixados por convencao de mercado (nunca otimizados em dados).

    - ATR 14 / RSI 14 / volume medio 20 / EMA 50: periodos classicos (Wilder, Bollinger, Elder).
    - `volume_multiple` 1.0: "acima da media" literal; gatilhos de volume usam multiplos proprios.
    - `rsi_veto` 75: acima disso a compra persegue preco esticado; veto, nao confluencia.
    - `min_confidence` 0.5: pelo menos metade das familias avaliadas confirmadas.
    """

    levels: LevelsConfig = LevelsConfig()
    atr_period: int = Field(default=14, ge=2)
    volume_period: int = Field(default=20, ge=2)
    volume_multiple: float = Field(default=1.0, gt=0)
    rsi_period: int = Field(default=14, ge=2)
    rsi_veto: float = Field(default=75.0, gt=50, le=100)
    trend_ema_period: int = Field(default=50, ge=2)
    continuity_window: int = Field(default=100, ge=2)
    min_confluences: int = Field(default=MIN_CONFLUENCES, ge=MIN_CONFLUENCES)
    min_confidence: float = Field(default=0.5, ge=0, le=1)


def to_decimal(value: float) -> Decimal:
    return Decimal(repr(float(value)))


def prepare(candles: Sequence[Candle], needed: int, continuity_window: int) -> CandleArrays | None:
    """Historico suficiente, so velas fechadas e sem buracos; senao None (na duvida, nao opera)."""
    if len(candles) < needed:
        return None
    if not all(c.closed for c in candles):
        return None
    if not candles_are_continuous(candles, candles[-1].timeframe, max(continuity_window, needed)):
        return None
    return CandleArrays.from_candles(candles)


def build_signal(
    *,
    name: str,
    candles: Sequence[Candle],
    cfg: CommonConfig,
    context: StrategyContext,
    atr_value: float,
    structure_stop: Decimal,
    checks: Mapping[str, bool],
    triggers: tuple[str, ...],
    summary: str,
    max_target: Decimal | None = None,
) -> StrategySignal | None:
    """Niveis (stop + alvo liquido de taxas), confluencia por familias e montagem do sinal."""
    last = candles[-1]
    levels = compute_levels(
        last.close, atr_value, structure_stop, cfg.levels, effective_fee_rate(cfg.levels, context)
    )
    if levels is None:
        return None
    stop, target = levels
    if max_target is not None and target > max_target:
        return None
    hits, families, confidence = score_confluences(checks)
    if len(families) < cfg.min_confluences or confidence < cfg.min_confidence:
        return None
    signal = Signal(
        symbol=last.symbol,
        timeframe=last.timeframe,
        created_at=last.close_time,
        strategy=name,
        entry=last.close,
        stop_loss=stop,
        take_profit=target,
        min_reward_risk=cfg.levels.reward_risk,
    )
    rationale = f"{summary} Confluencias independentes ({len(families)} familias): {', '.join(hits)}."
    return StrategySignal(
        signal=signal, confidence=confidence, rationale=rationale, triggers=triggers, confluences=hits
    )
