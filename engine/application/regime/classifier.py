import math
from collections.abc import Sequence
from dataclasses import dataclass

from engine.application.indicators import CandleArrays, adx, atr, bollinger, ema, sma
from engine.application.regime.models import (
    PROTECTIVE_REGIMES,
    Regime,
    RegimeConfig,
    RegimeMetrics,
    RegimeReading,
)
from engine.domain.candle import Candle

DEFAULT_CONFIG = RegimeConfig()


@dataclass(frozen=True, slots=True)
class _Raw:
    regime: Regime
    range_defined: bool
    metrics: RegimeMetrics | None


def _safe_ratio(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator > 0 else math.nan


def _range_stats(
    arrays: CandleArrays, i: int, atr_now: float, cfg: RegimeConfig
) -> tuple[float, int, int, bool]:
    lo = i - cfg.range_lookback + 1
    highs, lows = arrays.high[lo : i + 1], arrays.low[lo : i + 1]
    top, bottom = float(highs.max()), float(lows.min())
    height = top - bottom
    margin = cfg.range_touch_fraction * height
    touches_top = int((highs >= top - margin).sum())
    touches_bottom = int((lows <= bottom + margin).sum())
    height_atr = height / atr_now
    defined = (
        cfg.range_min_height_atr <= height_atr <= cfg.range_max_height_atr
        and touches_top >= cfg.range_min_touches
        and touches_bottom >= cfg.range_min_touches
    )
    return height_atr, touches_top, touches_bottom, defined


def _raw_regime(m: RegimeMetrics, cfg: RegimeConfig) -> Regime:
    # prioridade: o que impede operar (liquidez, volatilidade) vem antes de qualquer leitura direcional
    if m.volume_ratio < cfg.low_volume_ratio:
        return Regime.BAIXA_LIQUIDEZ
    if m.atr_ratio >= cfg.high_vol_atr_ratio or m.bb_width_ratio >= cfg.high_vol_bbw_ratio:
        return Regime.ALTA_VOLATILIDADE
    trending = m.adx >= cfg.adx_trend_min
    if trending and m.plus_di > m.minus_di and m.ema_fast_above_slow and m.ema_slope_atr >= cfg.slope_min_atr:
        return Regime.TENDENCIA_ALTA
    if (
        trending
        and m.minus_di > m.plus_di
        and not m.ema_fast_above_slow
        and m.ema_slope_atr <= -cfg.slope_min_atr
    ):
        return Regime.TENDENCIA_BAIXA
    if m.adx <= cfg.adx_lateral_max and abs(m.ema_slope_atr) < cfg.slope_min_atr:
        return Regime.LATERAL
    return Regime.INDEFINIDO


def _raw_series(arrays: CandleArrays, cfg: RegimeConfig) -> list[_Raw]:
    adx_out = adx(arrays.high, arrays.low, arrays.close, cfg.adx_period)
    atr_v = atr(arrays.high, arrays.low, arrays.close, cfg.atr_period)
    atr_base = sma(atr_v, cfg.baseline_lookback)
    bands = bollinger(arrays.close, cfg.bb_period, cfg.bb_std)
    bbw = (bands.upper - bands.lower) / bands.middle
    bbw_base = sma(bbw, cfg.baseline_lookback)
    fast, slow = ema(arrays.close, cfg.ema_fast), ema(arrays.close, cfg.ema_slow)
    vol_short, vol_long = sma(arrays.volume, cfg.volume_short), sma(arrays.volume, cfg.volume_long)
    lag = cfg.slope_lookback
    out: list[_Raw] = []
    for i in range(len(arrays)):
        needed = (adx_out.adx[i], atr_v[i], atr_base[i], bbw[i], bbw_base[i], slow[i], fast[i], vol_long[i])
        if i < cfg.min_history - 1 or any(math.isnan(float(v)) for v in needed):
            out.append(_Raw(Regime.INDEFINIDO, False, None))
            continue
        atr_now = float(atr_v[i])
        slope = (float(slow[i]) - float(slow[i - lag])) / (lag * atr_now) if atr_now > 0 else 0.0
        metrics = RegimeMetrics(
            adx=float(adx_out.adx[i]),
            plus_di=float(adx_out.plus_di[i]),
            minus_di=float(adx_out.minus_di[i]),
            atr_rel=atr_now / float(arrays.close[i]),
            atr_ratio=_safe_ratio(atr_now, float(atr_base[i])),
            bb_width=float(bbw[i]),
            bb_width_ratio=_safe_ratio(float(bbw[i]), float(bbw_base[i])),
            ema_slope_atr=slope,
            ema_fast_above_slow=bool(fast[i] > slow[i]),
            volume_ratio=_safe_ratio(float(vol_short[i]), float(vol_long[i])),
        )
        regime = _raw_regime(metrics, cfg)
        defined = False
        if regime is Regime.LATERAL and atr_now > 0:
            height, top, bottom, defined = _range_stats(arrays, i, atr_now, cfg)
            metrics = metrics.model_copy(
                update={"range_height_atr": height, "range_touches_top": top, "range_touches_bottom": bottom}
            )
        out.append(_Raw(regime, defined, metrics))
    return out


def is_operable(regime: Regime, range_defined: bool) -> bool:
    match regime:
        case Regime.TENDENCIA_ALTA | Regime.TENDENCIA_BAIXA:
            return True
        case Regime.LATERAL:
            return range_defined
        case _:
            return False


def _explain(regime: Regime, defined: bool, m: RegimeMetrics | None, cfg: RegimeConfig) -> str:
    if m is None:
        return f"histórico insuficiente (mínimo de {cfg.min_history} velas fechadas)"
    adx_txt = f"ADX {m.adx:.1f}"
    match regime:
        case Regime.TENDENCIA_ALTA | Regime.TENDENCIA_BAIXA:
            direction = "DI+ acima de DI-" if regime is Regime.TENDENCIA_ALTA else "DI- acima de DI+"
            return (
                f"{regime.label}: {adx_txt} (mínimo {cfg.adx_trend_min:.0f}), {direction}, "
                f"inclinação da média {m.ema_slope_atr:+.2f} ATR por vela"
            )
        case Regime.LATERAL if defined:
            return (
                f"lateral com faixa definida: {adx_txt} (máximo {cfg.adx_lateral_max:.0f}), faixa de "
                f"{m.range_height_atr or 0:.1f} ATR com {m.range_touches_top} toques no topo e "
                f"{m.range_touches_bottom} no fundo"
            )
        case Regime.LATERAL:
            return f"lateral sem faixa bem definida: {adx_txt}, sem operação"
        case Regime.ALTA_VOLATILIDADE:
            return (
                f"alta volatilidade: ATR {m.atr_ratio:.2f}x e largura das Bollinger {m.bb_width_ratio:.2f}x "
                "a média recente, sem operação"
            )
        case Regime.BAIXA_LIQUIDEZ:
            return f"baixa liquidez: volume recente em {m.volume_ratio:.0%} da média, sem operação"
        case _:
            return f"sem direção clara ({adx_txt} em zona intermediária), sem operação"


def classify_series(candles: Sequence[Candle], config: RegimeConfig = DEFAULT_CONFIG) -> list[RegimeReading]:
    """Uma leitura por vela fechada, usando APENAS velas ate ela (causal: prefixo => mesmas leituras).

    Histerese: o regime confirmado so muda apos `confirm_bars` velas seguidas com o mesmo novo regime bruto;
    ALTA_VOLATILIDADE e BAIXA_LIQUIDEZ entram na hora (protecao), mas a saida exige confirmacao.
    """
    if any(not c.closed for c in candles):
        raise ValueError("regime classification requires closed candles only")
    raws = _raw_series(CandleArrays.from_candles(candles), config)
    readings: list[RegimeReading] = []
    confirmed: Regime | None = None
    bars = 0
    pending: Regime | None = None
    pending_n = 0
    for candle, raw in zip(candles, raws, strict=True):
        if raw.metrics is None:
            confirmed, bars, pending, pending_n = None, 0, None, 0
            readings.append(
                RegimeReading(
                    at=candle.close_time,
                    regime=Regime.INDEFINIDO,
                    raw_regime=Regime.INDEFINIDO,
                    range_defined=False,
                    operable=False,
                    reason=_explain(Regime.INDEFINIDO, False, None, config),
                    bars_in_regime=0,
                )
            )
            continue
        if confirmed is None:
            confirmed, bars = raw.regime, 0
        elif raw.regime is confirmed:
            pending, pending_n = None, 0
        elif raw.regime in PROTECTIVE_REGIMES:
            confirmed, bars, pending, pending_n = raw.regime, 0, None, 0
        else:
            pending_n = pending_n + 1 if raw.regime is pending else 1
            pending = raw.regime
            if pending_n >= config.confirm_bars:
                confirmed, bars, pending, pending_n = raw.regime, 0, None, 0
        bars += 1
        defined = raw.range_defined and confirmed is Regime.LATERAL
        readings.append(
            RegimeReading(
                at=candle.close_time,
                regime=confirmed,
                raw_regime=raw.regime,
                range_defined=defined,
                operable=is_operable(confirmed, defined),
                reason=_explain(confirmed, defined, raw.metrics, config),
                bars_in_regime=bars,
                pending_regime=pending,
                pending_bars=pending_n,
                metrics=raw.metrics,
            )
        )
    return readings


def classify_regime(candles: Sequence[Candle], config: RegimeConfig = DEFAULT_CONFIG) -> RegimeReading:
    if not candles:
        raise ValueError("candles must not be empty")
    return classify_series(candles, config)[-1]
