"""Filtro de regime INVIOLAVEL do gate: nova entrada (long, spot) somente em TENDENCIA DE ALTA confirmada.

Regra de negocio: o robo nunca compra na queda nem no mercado lateral; na duvida, nao entra. Vive na camada de
risco (mesma que garante stop e limites), portanto vale para paper, backtest e o gateway live. Nao existe
opcao de desligar: `RiskConfig` so aceita limiares IGUAIS OU MAIS RIGIDOS que os pisos abaixo.

Criterios (todos sobre velas FECHADAS ate a ultima do historico; sem look-ahead):

Timeframe de operacao (TF):
  - ADX(14) >= `regime_adx_min` (piso 20, padrao 22) e DI+ > DI-;
  - EMA(20) > EMA(50) e fechamento > EMA(50);
  - inclinacao da EMA(50) em 10 velas, em ATR por vela, >= `regime_slope_min_atr` (piso 0,02, padrao 0,03);
  - razao de eficiencia de Kaufman (20 velas) >= `regime_er_min` (piso 0,20, padrao 0,25) com variacao liquida
    positiva: descarta mercado que oscila em canal (muito caminho, pouca distancia);
  - alta liquida em 20 velas >= `regime_rise_min_atr` ATR (piso 1,0, padrao 1,5).
Timeframe superior (HTF = 4 x TF, montado das proprias velas fechadas; so grupos completos entram):
  - EMA(10) > EMA(20), fechamento > EMA(20) e EMA(20) subindo.

Veredito: QUEDA se a media rapida esta abaixo da lenta com inclinacao negativa no TF, ou o mesmo no HTF;
LATERAL se ADX baixo, eficiencia baixa ou inclinacao ~0; ALTA se todos os criterios acima passam; qualquer
outro caso (mistos, historico curto, valores nao finitos) e INDEFINIDO e tambem NAO entra.
"""

import math
from collections.abc import Sequence
from enum import StrEnum

import numpy as np
from pydantic import Field

from engine.application.indicators import CandleArrays, adx, atr, ema
from engine.domain.base import DomainModel
from engine.domain.candle import Candle
from engine.domain.risk import (
    REGIME_ADX_FLOOR,
    REGIME_ER_FLOOR,
    REGIME_RISE_FLOOR,
    REGIME_SLOPE_FLOOR,
    RiskConfig,
)
from engine.domain.types import UtcDatetime

ADX_PERIOD = 14
EMA_FAST = 20
EMA_SLOW = 50
SLOPE_LOOKBACK = 10
ER_LOOKBACK = 20
HTF_FACTOR = 4
HTF_FAST = 10
HTF_SLOW = 20
HTF_SLOPE_LOOKBACK = 3
MIN_TF_CANDLES = EMA_SLOW + SLOPE_LOOKBACK
MIN_HTF_CANDLES = HTF_SLOW + HTF_SLOPE_LOOKBACK
# Os grupos do HTF alinham ao relogio: no pior caso o 1o grupo vem cortado (ate FACTOR-1 velas perdidas), por
# isso `MIN_HTF * FACTOR` velas nem sempre bastam; com a folga abaixo sempre ha MIN_HTF grupos completos.
MIN_CANDLES = max(MIN_TF_CANDLES, MIN_HTF_CANDLES * HTF_FACTOR + HTF_FACTOR - 1)
# O veredito usa SEMPRE as ultimas REGIME_WINDOW velas: o aquecimento das EMAs/ADX depende do tamanho da
# janela, entao fixa-la faz backtest (janela de `history_window`) e paper/live darem a mesma leitura.
REGIME_WINDOW = 400
_last: tuple[object, "RegimeEvidence"] | None = None


class RegimeVerdict(StrEnum):
    UPTREND = "uptrend"
    DOWNTREND = "downtrend"
    SIDEWAYS = "sideways"
    UNKNOWN = "unknown"


class RegimeEvidence(DomainModel):
    """Evidencia do regime avaliado, selada junto da aprovacao e exibida nas decisoes."""

    verdict: RegimeVerdict
    at: UtcDatetime | None = None
    reason: str = Field(min_length=1)
    adx: float | None = None
    plus_di: float | None = None
    minus_di: float | None = None
    efficiency: float | None = None
    slope_atr: float | None = None
    rise_atr: float | None = None
    htf_up: bool | None = None
    htf_slope_atr: float | None = None

    @property
    def allows_entry(self) -> bool:
        return self.verdict is RegimeVerdict.UPTREND

    @property
    def plain_text(self) -> str:
        label = {
            RegimeVerdict.UPTREND: "Mercado em alta",
            RegimeVerdict.DOWNTREND: "Mercado em queda",
            RegimeVerdict.SIDEWAYS: "Mercado lateral",
            RegimeVerdict.UNKNOWN: "Mercado sem direção clara",
        }[self.verdict]
        return f"{label} — o robô só entra na alta"

    def to_context(self) -> dict[str, object]:
        return self.model_dump(mode="json", exclude={"at"}) | {"at": self.at.isoformat() if self.at else None}


def _unknown(reason: str, at: UtcDatetime | None = None) -> RegimeEvidence:
    return RegimeEvidence(verdict=RegimeVerdict.UNKNOWN, reason=reason, at=at)


def resample_closed(candles: Sequence[Candle], factor: int = HTF_FACTOR) -> list[tuple[float, float, float]]:
    """(high, low, close) dos grupos COMPLETOS de `factor` velas alinhados ao relogio; o grupo em andamento
    (incompleto) fica de fora, entao o HTF nunca enxerga o futuro."""
    if not candles:
        return []
    bucket_seconds = candles[0].timeframe.seconds * factor
    groups: dict[int, list[Candle]] = {}
    for c in candles:
        groups.setdefault(int(c.open_time.timestamp()) // bucket_seconds, []).append(c)
    return [
        (
            max(float(c.high) for c in group),
            min(float(c.low) for c in group),
            float(group[-1].close),
        )
        for _, group in sorted(groups.items())
        if len(group) == factor
    ]


def _htf(candles: Sequence[Candle], slope_min: float) -> tuple[bool, bool, float] | None:
    """(alta, queda, inclinacao em ATR) do HTF, ou None se faltam grupos completos."""
    bars = resample_closed(candles)
    if len(bars) < MIN_HTF_CANDLES:
        return None
    high, low, close = (np.array(col, dtype=np.float64) for col in zip(*bars, strict=True))
    fast, slow = ema(close, HTF_FAST), ema(close, HTF_SLOW)
    atr_now = float(atr(high, low, close)[-1])
    delta = float(slow[-1] - slow[-1 - HTF_SLOPE_LOOKBACK])
    if not all(math.isfinite(v) for v in (float(fast[-1]), float(slow[-1]), atr_now, delta)) or atr_now <= 0:
        return None
    slope = delta / (HTF_SLOPE_LOOKBACK * atr_now)
    above = float(fast[-1]) > float(slow[-1])
    up = above and float(close[-1]) > float(slow[-1]) and slope > 0
    down = (not above) and slope <= -slope_min
    return up, down, slope


def assess_regime(history: Sequence[Candle], config: RiskConfig) -> RegimeEvidence:
    """Cache de 1 entrada (insight do robo + gate sobre as mesmas velas); a chave sao as proprias velas e os
    limiares, entao nada vindo de fora altera o veredito."""
    window = history[-REGIME_WINDOW:]
    key = (
        tuple((c.symbol, c.open_time, c.open, c.high, c.low, c.close, c.closed) for c in window),
        config.regime_adx_min,
        config.regime_er_min,
        config.regime_slope_min_atr,
        config.regime_rise_min_atr,
    )
    global _last
    if (hit := _last) is not None and hit[0] == key:
        return hit[1]
    result = _assess(window, config)
    _last = (key, result)
    return result


def _assess(history: Sequence[Candle], config: RiskConfig) -> RegimeEvidence:
    """Classifica o mercado na ultima vela fechada de `history`. Deterministica e causal (prefixo => mesmo
    resultado). Os limiares da config nunca ficam abaixo dos pisos, mesmo que a config seja contornada."""
    if len(history) < MIN_CANDLES:
        return _unknown(
            f"historico insuficiente para avaliar o regime ({len(history)} de {MIN_CANDLES} velas)"
        )
    if any(not c.closed for c in history):
        return _unknown("historico com vela nao fechada")
    at = history[-1].close_time
    adx_min = max(float(config.regime_adx_min), REGIME_ADX_FLOOR)
    er_min = max(float(config.regime_er_min), REGIME_ER_FLOOR)
    slope_min = max(float(config.regime_slope_min_atr), REGIME_SLOPE_FLOOR)
    rise_min = max(float(config.regime_rise_min_atr), REGIME_RISE_FLOOR)

    arrays = CandleArrays.from_candles(list(history))
    close = arrays.close
    trend = adx(arrays.high, arrays.low, close, ADX_PERIOD)
    atr_now = float(atr(arrays.high, arrays.low, close)[-1])
    fast, slow = ema(close, EMA_FAST), ema(close, EMA_SLOW)
    adx_v, plus, minus = float(trend.adx[-1]), float(trend.plus_di[-1]), float(trend.minus_di[-1])
    fast_v, slow_v, last = float(fast[-1]), float(slow[-1]), float(close[-1])
    slow_prev = float(slow[-1 - SLOPE_LOOKBACK])
    window = close[-ER_LOOKBACK - 1 :]
    path = float(np.abs(np.diff(window)).sum())
    net = float(window[-1] - window[0])
    values = (adx_v, plus, minus, fast_v, slow_v, last, slow_prev, atr_now, path, net)
    if not all(math.isfinite(v) for v in values) or atr_now <= 0:
        return _unknown("indicadores indisponiveis ou invalidos", at)
    slope = (slow_v - slow_prev) / (SLOPE_LOOKBACK * atr_now)
    efficiency = abs(net) / path if path > 0 else 0.0
    rise = net / atr_now
    htf = _htf(history, slope_min)
    fields = {
        "at": at,
        "adx": adx_v,
        "plus_di": plus,
        "minus_di": minus,
        "efficiency": efficiency,
        "slope_atr": slope,
        "rise_atr": rise,
        "htf_up": None if htf is None else htf[0],
        "htf_slope_atr": None if htf is None else htf[2],
    }
    if htf is None:
        return RegimeEvidence(
            verdict=RegimeVerdict.UNKNOWN,
            reason="timeframe superior sem velas completas suficientes",
            **fields,
        )
    htf_up, htf_down, _ = htf
    if (fast_v < slow_v and slope <= -slope_min) or htf_down:
        where = "no tempo gráfico superior" if htf_down else "no tempo gráfico de operação"
        return RegimeEvidence(verdict=RegimeVerdict.DOWNTREND, reason=f"mercado em queda {where}", **fields)
    if adx_v < adx_min or efficiency < er_min or abs(slope) < slope_min:
        return RegimeEvidence(
            verdict=RegimeVerdict.SIDEWAYS,
            reason=f"mercado lateral: ADX {adx_v:.1f} (mín. {adx_min:.0f}), eficiência {efficiency:.2f} "
            f"(mín. {er_min:.2f}), inclinação {slope:+.2f} (mín. {slope_min:.2f})",
            **fields,
        )
    confirmed = (
        plus > minus
        and fast_v > slow_v
        and last > slow_v
        and slope >= slope_min
        and net > 0
        and rise >= rise_min
        and htf_up
    )
    if not confirmed:
        return RegimeEvidence(
            verdict=RegimeVerdict.UNKNOWN, reason="sinais mistos: alta não confirmada", **fields
        )
    return RegimeEvidence(
        verdict=RegimeVerdict.UPTREND,
        reason=f"mercado em alta: ADX {adx_v:.1f}, eficiência {efficiency:.2f}, "
        f"inclinação {slope:+.2f} ATR/vela, alta de {rise:.1f} ATR em {ER_LOOKBACK} velas, "
        "tempo gráfico superior em alta",
        **fields,
    )
