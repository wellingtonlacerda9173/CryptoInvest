"""Backtest com o filtro de regime REAL (sem o stub autouse): queda e lateral nao entram; alta entra.

Trocar `assess_regime` no caminho do backtest (gate) quebra estes testes."""

from decimal import Decimal

import pytest

from engine.application.backtest import BacktestEngine
from engine.application.backtest.models import BacktestResult
from engine.application.risk.regime_filter import MIN_CANDLES
from engine.domain.candle import Candle
from tests.unit.backtest.engine_helpers import FIXED_NOW, Scripted, mk, settings

pytestmark = pytest.mark.real_regime

COUNT = 260
SIGNALS = set(range(MIN_CANDLES + 5, COUNT - 1, 4))


def series(drift: float) -> list[Candle]:
    out = []
    for i in range(COUNT):
        mid = 100.0 + drift * i + (0.0 if drift else 1.5 * (-1) ** i)
        out.append(mk(i, f"{mid:.4f}", f"{mid + 0.8:.4f}", f"{mid - 0.8:.4f}", f"{mid:.4f}"))
    return out


def levels(candle: Candle) -> tuple[str, str, str]:
    close = candle.close
    return str(close), str(close * Decimal("0.97")), str(close * Decimal("1.12"))


async def backtest(drift: float) -> BacktestResult:
    scripted = Scripted(set(SIGNALS), levels)
    return await BacktestEngine([scripted], settings(taker_fee_rate="0"), clock=lambda: FIXED_NOW).run(
        series(drift)
    )


@pytest.mark.parametrize(("drift", "code"), [(-0.2, "regime_downtrend"), (0.0, "regime_sideways")])
async def test_downtrend_and_sideways_never_enter(drift: float, code: str) -> None:
    result = await backtest(drift)

    assert not result.trades
    refusals = [d.code for d in result.decisions if d.code.startswith("regime_")]
    assert refusals
    assert set(refusals) == {code}


async def test_uptrend_enters() -> None:
    result = await backtest(0.2)

    assert result.trades
    assert not [d for d in result.decisions if d.code.startswith("regime_")]


async def test_verdict_at_each_bar_ignores_future_bars() -> None:
    cut = 200
    up = series(0.2)
    crash = [*up[:cut], *(c.model_copy(update={"close": c.close / 3, "low": c.low / 3}) for c in up[cut:])]
    engine = lambda: BacktestEngine(  # noqa: E731
        [Scripted(set(SIGNALS), levels)], settings(taker_fee_rate="0"), clock=lambda: FIXED_NOW
    )

    a, b = await engine().run(up), await engine().run(crash)

    def seen(result: BacktestResult) -> list[tuple[object, ...]]:
        return [
            (d.at, d.code, d.message, d.entry, d.context)
            for d in result.decisions
            if d.at <= up[cut - 1].close_time
        ]

    assert seen(a)
    assert seen(a) == seen(b)
    assert {d.code for d in b.decisions if d.at > up[cut - 1].close_time} & {"regime_downtrend"}
