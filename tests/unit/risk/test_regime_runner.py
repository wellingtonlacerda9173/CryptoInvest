"""Filtro de regime no PaperRobot (avaliador REAL): so a alta aprova; queda e lateral viram decisao clara."""

import pytest
from pydantic import ValidationError

from engine.application.backtest import BacktestSettings
from engine.application.risk import regime_filter
from engine.application.risk.regime_filter import MIN_CANDLES
from tests.unit.backtest.engine_helpers import mk
from tests.unit.runner.fakes import FakeClock, Harness, config

N = 130
SIGNAL = ("100", "100.5", "99.5", "100")


def series(drift: float) -> list:  # type: ignore[type-arg]
    out = []
    for i in range(N):
        mid = 100.0 - drift * (N - i) + (0.0 if drift else 1.5 * (-1) ** i)
        out.append(mk(i, f"{mid:.4f}", f"{mid + 0.8:.4f}", f"{mid - 0.8:.4f}", f"{mid:.4f}"))
    return out


async def decide(drift: float) -> Harness:
    candles = series(drift)
    clock = FakeClock(candles[-1].close_time)
    h = await Harness({N}, history=candles, cfg=config(history_window=400), clock=clock).start()
    await h.close(mk(N, *SIGNAL))
    return h


@pytest.mark.parametrize(
    ("drift", "code", "phrase"),
    [
        (0.2, "approved", "Mercado em alta"),
        (-0.2, "regime_downtrend", "Mercado em queda"),
        (0.0, "regime_sideways", "Mercado lateral"),
    ],
)
async def test_only_uptrend_places_an_order(drift: float, code: str, phrase: str) -> None:
    h = await decide(drift)

    decision = h.journal.decisions[-1]
    assert decision.code == code
    assert decision.insight is not None
    assert phrase in decision.insight["regime"]
    assert (decision.insight["regime_gate"]["verdict"] == "uptrend") is (code == "approved")
    assert bool(h.journal.orders) is (code == "approved")
    status = h.robot.status_payload(h.clock.now())
    assert phrase in status["market_regime"]["BTC/USDT"]["text"]


async def test_robot_assesses_the_regime_once_per_candle(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    original = regime_filter._assess
    monkeypatch.setattr(regime_filter, "_assess", lambda *a: calls.append(1) or original(*a))

    h = await decide(0.2)

    assert h.journal.decisions[-1].code == "approved"
    assert len(calls) == 1


def test_history_window_below_the_regime_minimum_is_refused() -> None:
    assert config().history_window >= MIN_CANDLES
    with pytest.raises(ValidationError, match="history_window"):
        config(history_window=MIN_CANDLES - 1)
    with pytest.raises(ValidationError, match="history_window"):
        BacktestSettings(history_window=MIN_CANDLES - 1)
    assert config(history_window=MIN_CANDLES).history_window == MIN_CANDLES
    assert BacktestSettings().history_window >= MIN_CANDLES
