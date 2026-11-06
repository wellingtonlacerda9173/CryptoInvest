"""Estudo de `pullback_trend` (docs/pullback_trend.md). Pesquisa, NUNCA producao.

Uso: `uv run python scripts/pullback_trend_study.py --out reports/pullback_trend/results.json`

O cenario "sem filtro" troca `assess_regime` por um veredito fixo de ALTA, em memoria e so neste processo, o
mesmo mecanismo do stub de testes (tests/unit/regime_stub.py); serve apenas de referencia. Nada em `engine/`
oferece essa opcao e este script nao e importado por nenhum modulo do robo.
"""

import argparse
import asyncio
import json
import math
import sys
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import product
from pathlib import Path
from statistics import mean, stdev
from typing import Any

from engine.application.backtest import BacktestEngine, BacktestSettings, compute_metrics, default_rules
from engine.application.risk import regime_filter
from engine.application.risk.regime_filter import RegimeEvidence, RegimeVerdict
from engine.application.strategies import (
    ElephantBarBreakout,
    EmaCrossTrend,
    LevelsConfig,
    PullbackTrend,
    PullbackTrendConfig,
    Strategy,
)
from engine.domain.candle import Candle
from engine.domain.risk import RiskConfig
from engine.domain.types import Timeframe
from engine.runner.candle_source import read_cache

SYMBOLS = ("BTC/USDT", "ETH/USDT")
TIMEFRAMES = (Timeframe.H1, Timeframe.H4)
PERIOD_END_1H = datetime(2026, 10, 18, 19, tzinfo=UTC)
YEARS = 2
IN_SAMPLE_SHARE = 0.6
WARMUP = 400
MIN_SIGNIFICANT_TRADES = 100
GRID_PULLBACK_BARS = (3, 5, 8)
GRID_REWARD_RISK = (Decimal(2), Decimal(3))
FORCED = RegimeEvidence(verdict=RegimeVerdict.UPTREND, reason="referencia: regime forcado em alta")


@contextmanager
def forced_uptrend() -> Iterator[None]:
    originals: list[tuple[Any, Callable[..., RegimeEvidence]]] = []
    real = regime_filter.assess_regime
    for name, module in list(sys.modules.items()):
        if name.startswith("engine.") and getattr(module, "assess_regime", None) is real:
            originals.append((module, real))
            module.assess_regime = lambda *_a, **_k: FORCED
    try:
        yield
    finally:
        for module, original in originals:
            module.assess_regime = original


def pullback(bars: int = 5, reward_risk: Decimal = Decimal(2)) -> Strategy:
    levels = LevelsConfig(stop_buffer_atr=0.0, min_risk_atr=0.5, reward_risk=reward_risk)
    return PullbackTrend(PullbackTrendConfig(pullback_bars=bars, levels=levels))


def max_loss_streak(pnls: list[Decimal]) -> int:
    best = run = 0
    for pnl in pnls:
        run = run + 1 if pnl < 0 else 0
        best = max(best, run)
    return best


def expectancy_ci(rs: list[float]) -> tuple[float, float] | None:
    if len(rs) < 3:
        return None
    half = 1.96 * stdev(rs) / math.sqrt(len(rs))
    return mean(rs) - half, mean(rs) + half


@dataclass(frozen=True)
class Series:
    symbol: str
    timeframe: Timeframe
    candles: list[Candle]
    start: int
    split: int

    @property
    def label(self) -> str:
        return f"{self.symbol.replace('/', '')}_{self.timeframe.value}"


def load_series(symbol: str, timeframe: Timeframe, cache: Path) -> Series:
    candles = read_cache(cache / f"{symbol.replace('/', '')}_{timeframe.value}.csv", symbol, timeframe)
    end = min(PERIOD_END_1H, candles[-1].close_time)
    begin = end - timedelta(days=365 * YEARS)
    in_period = [i for i, c in enumerate(candles) if c.open_time >= begin and c.close_time <= end]
    first, last = in_period[0], in_period[-1]
    if first < WARMUP:
        raise SystemExit(f"{symbol} {timeframe.value}: cache sem {WARMUP} velas de aquecimento")
    window = candles[first - WARMUP : last + 1]
    length = last - first + 1
    return Series(symbol, timeframe, window, WARMUP, WARMUP + round(length * IN_SAMPLE_SHARE))


def settings() -> BacktestSettings:
    # sem halt por drawdown: mesmo tratamento do comparativo de docs/regime_filter.md
    return BacktestSettings(drawdown_halt=False, risk=RiskConfig())


async def run_one(strategy: Strategy, series: Series, segment: str, *, regime: str) -> dict[str, Any]:
    cfg = settings()
    rules = default_rules(series.symbol)
    if segment == "ajuste":
        candles, start = series.candles[: series.split], series.start
    else:
        candles, start = series.candles, series.split
    now = candles[-1].close_time
    engine = BacktestEngine([strategy], cfg, rules=rules, clock=lambda: now)
    if regime == "sem_filtro":
        with forced_uptrend():
            result = await engine.run(candles, start=start)
    else:
        result = await engine.run(candles, start=start)
    metrics = compute_metrics(result)
    trips = sorted(result.round_trips, key=lambda t: t.closed_at)
    rs = [float(t.r_multiple) for t in trips]
    codes = Counter(d.code for d in result.decisions)
    first, last = candles[start], candles[-1]
    return {
        "estrategia": strategy.name,
        "segmento": segment,
        "regime": regime,
        "periodo": f"{first.open_time:%Y-%m-%d} a {last.close_time:%Y-%m-%d}",
        "velas": result.candles,
        "trades": len(trips),
        "expectativa_r": metrics.expectancy_r,
        "ic95_r": expectancy_ci(rs),
        "retorno": metrics.total_return,
        "drawdown_max": metrics.max_drawdown,
        "taxas_usdt": float(metrics.fees_paid),
        "taxa_acerto": metrics.win_rate,
        "fator_lucro": metrics.profit_factor,
        "maior_sequencia_perdas": max_loss_streak([t.pnl for t in trips]),
        "abertas_no_fim": result.open_at_end,
        "sem_significancia": len(trips) < MIN_SIGNIFICANT_TRADES,
        "decisoes": dict(codes),
        "saidas": dict(Counter(t.labels[-1] for t in trips)),
        "buy_and_hold": float(last.close / first.open - 1),
    }


def grid_variants() -> Iterator[tuple[str, Strategy]]:
    for bars, rr in product(GRID_PULLBACK_BARS, GRID_REWARD_RISK):
        yield f"pullback_trend[pullBars={bars},rr={rr}]", pullback(bars, rr)


def comparison_variants() -> Iterator[tuple[str, Strategy]]:
    yield "pullback_trend[pullBars=5,rr=2]", pullback()
    yield "ema_cross_trend", EmaCrossTrend()
    yield "elephant_bar_breakout", ElephantBarBreakout()


async def main_async(args: argparse.Namespace) -> None:
    results: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for symbol, timeframe in product(SYMBOLS, TIMEFRAMES):
        series = load_series(symbol, timeframe, args.cache_dir)
        print(f"== {series.label}: {len(series.candles) - series.start} velas", flush=True)
        for kind, variants in (("comparativo", comparison_variants), ("grade", grid_variants)):
            for label, strategy in variants():
                for segment, regime in product(("ajuste", "teste"), ("filtro_real", "sem_filtro")):
                    key = (series.label, label, segment, regime, "")
                    if key in seen:
                        continue
                    seen.add(key)
                    row = await run_one(strategy, series, segment, regime=regime)
                    row |= {"serie": series.label, "variante": label, "grupo": kind}
                    results.append(row)
                    print(
                        f"{series.label} {label} {segment} {regime}: trades={row['trades']} "
                        f"E[R]={row['expectativa_r']}",
                        flush=True,
                    )
                    args.out.write_text(json.dumps(results, indent=1, default=str), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, default=Path("data/cache"))
    args = parser.parse_args()
    if args.out.exists():
        raise SystemExit(f"{args.out} ja existe; escolha outro caminho")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
