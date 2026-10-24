"""Calibracao/poder do validador pelo fluxo real (BacktestEngine + coleta + walk-forward + nulas + veredito).

Serie sintetica: passeio aleatorio com uma vela-marcador de volume a cada `SPACING` velas. A estrategia de
teste entra em TODO marcador (stop 1%, alvo 3%). Com probabilidade `p` o preco "confirma" (sobe +3.3% em 2
velas), senao cai (-1.6%): o edge real e ~ 4p - 1 R por trade acima do acaso. Sem plantio (`p=None`) a mesma
estrategia opera um passeio aleatorio puro (nulo).
"""

import random
import statistics
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import NamedTuple

import pytest

from engine.application.backtest import BacktestSettings
from engine.application.regime import RegimeConfig
from engine.application.selector import SelectorConfig
from engine.application.strategies import StrategyContext, StrategySignal
from engine.application.validation import (
    ValidationConfig,
    Verdict,
    corrected_alpha,
    min_attainable_p,
    run_validation,
)
from engine.application.validation.models import ValidationReport
from engine.domain.candle import Candle
from engine.domain.signal import Signal
from engine.domain.types import Timeframe

START = datetime(2025, 1, 1, tzinfo=UTC)
PER_DAY = 6
SPACING = 8
NOISE = 0.005
MARKER_VOLUME = Decimal(600_000)
WIN_PATH = (0.010, 0.023, -0.016, -0.017)
LOSS_PATH = (-0.016, 0.008, 0.008, 0.0)
FAMILY_CHECKS = ("volume_forte", "preco_acima_da_ema_de_tendencia", "rsi_com_espaco", "sem_exaustao")
FAMILY_SIZE = 75
CONFIG = ValidationConfig(
    train_days_min=30,
    test_days=45,
    simulations=3_000,
    extra_comparisons=FAMILY_SIZE - 2,
    selector=SelectorConfig(min_trades=30),
)
DAYS = 165


def series(p: float | None, seed: int, days: int = DAYS) -> list[Candle]:
    rng = random.Random(seed)  # noqa: S311
    price, candles, count = 100.0, [], days * PER_DAY
    moves: dict[int, float] = {}
    for t in range(40, count - 5, SPACING):
        if p is not None:
            path = WIN_PATH if rng.random() < p else LOSS_PATH
            moves |= {t + 1 + k: step for k, step in enumerate(path)}
    for i in range(count):
        move = moves.get(i, rng.gauss(0.0, NOISE))
        close = price * (1 + move)
        marker = i >= 40 and (i - 40) % SPACING == 0
        candles.append(
            Candle(
                symbol="BTC/USDT",
                timeframe=Timeframe.H4,
                open_time=START + timedelta(seconds=Timeframe.H4.seconds * i),
                open=Decimal(f"{price:.4f}"),
                high=Decimal(f"{max(price, close) * 1.0004:.4f}"),
                low=Decimal(f"{min(price, close) * 0.9996:.4f}"),
                close=Decimal(f"{close:.4f}"),
                volume=MARKER_VOLUME if marker else Decimal(200_000),
                closed=True,
            )
        )
        price = close
    return candles


class MarkerEntry:
    name = "marcador"

    def evaluate(self, candles: Sequence[Candle], context: StrategyContext) -> StrategySignal | None:
        last = candles[-1]
        if last.volume != MARKER_VOLUME:
            return None
        signal = Signal(
            symbol=last.symbol,
            timeframe=last.timeframe,
            created_at=last.close_time,
            strategy=self.name,
            entry=last.close,
            stop_loss=(last.close * Decimal("0.99")).quantize(Decimal("0.0001")),
            take_profit=(last.close * Decimal("1.03")).quantize(Decimal("0.0001")),
        )
        return StrategySignal(
            signal=signal,
            confidence=1.0,
            rationale="marcador plantado",
            triggers=("barra_elefante_alta",),
            confluences=FAMILY_CHECKS,
        )


async def validate(p: float | None, seed: int) -> ValidationReport:
    return await run_validation(
        series(p, seed),
        [MarkerEntry()],
        BacktestSettings(),
        CONFIG.model_copy(update={"regime": RegimeConfig()}),
    )


class Cell(NamedTuple):
    p: float | None
    runs: int
    detected: int
    trades: float
    edge: float

    @property
    def rate(self) -> float:
        return self.detected / self.runs


async def sweep(p: float | None, seeds: range) -> Cell:
    reports = [(await validate(p, seed)).strategies[0] for seed in seeds]
    edges = [
        e.stats.mean_r - e.random_reference.mean_r_median
        for e in reports
        if e.random_reference and e.stats.mean_r is not None
    ]
    return Cell(
        p=p,
        runs=len(reports),
        detected=sum(e.judgement.verdict is Verdict.SUPERA for e in reports),
        trades=statistics.mean(e.stats.trades for e in reports),
        edge=statistics.mean(edges),
    )


@pytest.mark.slow
async def test_detection_rate_grows_with_edge() -> None:
    cells = [await sweep(p, range(100, 106)) for p in (0.5, 0.55, 0.6, 0.7)]
    print("\n  p(confirma)  edge vs acaso (R)  trades OOS  detecao")
    for c in cells:
        print(f"  {c.p:>10}  {c.edge:>16.2f}  {c.trades:>10.0f}  {c.detected}/{c.runs}")
    rates = [c.rate for c in cells]
    assert rates == sorted(rates)
    assert cells[-1].rate >= 5 / 6 and cells[0].rate <= 0.5


@pytest.mark.slow
async def test_no_edge_random_walk_has_no_false_positives() -> None:
    null = await sweep(None, range(200, 220))
    print(
        f"\n  passeio aleatorio: {null.detected}/{null.runs} falsos positivos, {null.trades:.0f} trades OOS"
    )
    alpha_c = corrected_alpha(CONFIG.alpha, FAMILY_SIZE, CONFIG.correction)
    assert null.detected <= max(1, round(alpha_c * null.runs))


def test_corrected_alpha_is_reachable_with_configured_simulations() -> None:
    assert min_attainable_p(CONFIG.simulations) < corrected_alpha(
        CONFIG.alpha, FAMILY_SIZE, CONFIG.correction
    )
