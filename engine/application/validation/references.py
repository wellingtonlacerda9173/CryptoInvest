from collections.abc import Sequence
from typing import NamedTuple

import numpy as np

from engine.application.selector import TradeSample
from engine.application.validation.models import BuyHold, RandomReference, TradeStats, ValidationConfig
from engine.domain.candle import Candle

Window = tuple[int, int]
DEFAULT_COST_PCT = 0.0025


def _peak_to_trough(increments: Sequence[float]) -> float:
    peak = level = worst = 0.0
    for step in increments:
        level += step
        peak = max(peak, level)
        worst = max(worst, peak - level)
    return worst


def trade_stats(samples: Sequence[TradeSample]) -> TradeStats:
    ordered = sorted(samples, key=lambda s: s.closed_at)
    r = [s.r_multiple for s in ordered]
    count = len(ordered)
    return TradeStats(
        trades=count,
        mean_r=sum(r) / count if count else None,
        total_r=sum(r),
        max_drawdown_r=_peak_to_trough(r),
        total_return=sum(s.return_frac for s in ordered),
        max_drawdown=_peak_to_trough([s.return_frac for s in ordered]),
        win_rate=sum(1 for x in r if x > 0) / count if count else None,
    )


def buy_and_hold(candles: Sequence[Candle], windows: Sequence[Window]) -> BuyHold:
    """Comprar na abertura e segurar durante CADA janela de teste (100% exposto), encadeando os retornos."""
    equity, peak, worst, used = 1.0, 1.0, 0.0, 0
    for start, end in windows:
        base = float(candles[start].open)
        window_start = equity
        for candle in candles[start:end]:
            level = window_start * float(candle.close) / base
            peak, worst = max(peak, level), max(worst, 1 - level / max(peak, level))
        equity = window_start * float(candles[end - 1].close) / base
        used += end - start
    return BuyHold(total_return=equity - 1.0, max_drawdown=worst, candles=used)


class _Series(NamedTuple):
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray

    @classmethod
    def of(cls, candles: Sequence[Candle]) -> "_Series":
        return cls(*(np.array([float(getattr(c, f)) for c in candles]) for f in cls._fields))


def _outcomes(
    series: _Series, bounds: Window, stop_pct: float, target_pct: float, config: ValidationConfig
) -> np.ndarray:
    """R de uma entrada a mercado na abertura de cada vela da janela, com o stop/alvo do trade real.

    Mesmas regras pessimistas do backtest: na vela de entrada so vale o stop, stop antes do alvo na mesma
    vela, gap no stop executa na abertura, fim do horizonte (ou dos dados) sai no fechamento; custo fixo.

    LIMITACAO (documentada no summary.md): stop e alvo sao FIXOS, sem saida parcial, breakeven nem trailing
    (a gestao real de `plan_partial`/`manage_position` e em Decimal e por posicao: simular cada entrada da
    janela por trade real custaria ordens de grandeza mais). A referencia aleatoria, portanto, e uma regua
    aproximada: a gestao real troca parte do alvo por saidas menores, o que muda a distribuicao de R.
    """
    open_, high, low, close = series
    last = len(close) - 1
    entries = np.arange(*bounds)
    price = open_[entries]
    stop, target = price * (1 - stop_pct), price * (1 + target_pct)
    ret = np.full(len(entries), np.nan)
    for k in range(config.horizon_candles + 1):
        idx = entries + k
        beyond = idx > last
        at = np.minimum(idx, last)
        alive = np.isnan(ret)
        stopped = alive & ~beyond & (low[at] <= stop)
        ret[stopped] = (np.minimum(stop, open_[at]) / price - 1)[stopped]
        hit = alive & ~stopped & ~beyond & (high[at] >= target) & (k > 0)
        ret[hit] = (target / price - 1)[hit]
        done = np.isnan(ret) & (beyond | (k == config.horizon_candles))
        ret[done] = (close[at] / price - 1)[done]
    cost = DEFAULT_COST_PCT if config.cost_pct is None else config.cost_pct
    return (ret - cost) / stop_pct


def _window_of(candles: Sequence[Candle], windows: Sequence[Window], sample: TradeSample) -> Window | None:
    return next(
        (w for w in windows if candles[w[0]].open_time <= sample.opened_at < candles[w[1] - 1].close_time),
        None,
    )


def random_reference(
    candles: Sequence[Candle],
    windows: Sequence[Window],
    samples: Sequence[TradeSample],
    config: ValidationConfig,
) -> RandomReference | None:
    """Entradas aleatorias com a MESMA contagem por janela e o MESMO stop/alvo de cada trade real.

    Distribuicao da expectativa em R sob `config.simulations` sorteios (semente fixa) e p-valores unilaterais
    (com correcao +1) de a expectativa real ser maior/menor que a do acaso.
    """
    usable = [
        (s, w)
        for s in samples
        if s.stop_pct is not None and (w := _window_of(candles, windows, s)) is not None
    ]
    if not usable:
        return None
    series = _Series.of(candles)
    rng = np.random.default_rng(config.seed)
    totals = np.zeros(config.simulations)
    for sample, bounds in usable:
        assert sample.stop_pct is not None  # noqa: S101
        stop_pct = sample.stop_pct
        outcomes = _outcomes(series, bounds, stop_pct, sample.target_pct or 2 * stop_pct, config)
        totals += outcomes[rng.integers(0, len(outcomes), config.simulations)]
    means = totals / len(usable)
    real = float(np.mean([s.r_multiple for s, _ in usable]))
    p05, median, p95 = (float(x) for x in np.percentile(means, [5, 50, 95]))
    denominator = config.simulations + 1
    return RandomReference(
        simulations=config.simulations,
        seed=config.seed,
        mean_r_p05=p05,
        mean_r_median=median,
        mean_r_p95=p95,
        real_mean_r=real,
        p_better=(1 + int((means >= real).sum())) / denominator,
        p_worse=(1 + int((means <= real).sum())) / denominator,
    )
