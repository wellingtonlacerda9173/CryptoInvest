"""Paridade: as mesmas velas pelo runner (relogio fake, latencia equivalente) e pelo BacktestEngine."""

import math
import random
from collections.abc import Callable, Sequence
from decimal import Decimal
from itertools import pairwise

import pytest

from engine.application.backtest import BacktestEngine
from engine.application.strategies import ElephantBarBreakout, EmaCrossTrend, Strategy
from engine.domain.candle import Candle
from engine.domain.risk import RiskConfig
from engine.domain.trade import Trade
from tests.unit.backtest.engine_helpers import FIXED_NOW, Scripted, mk, settings
from tests.unit.runner.fakes import Harness, config, make_book, warmup

D = Decimal
START = 29
CENT = D("0.01")
# cotacao e book nao participam do que se compara; limites folgados evitam vetos por artefato do teste
RISK = RiskConfig(max_entry_deviation=D("0.02"), max_spread=D("0.05"))
TRADE_KEYS = {"symbol", "entry_price", "exit_price", "quantity", "exit_reason", "opened_at", "closed_at"}


def random_walk(count: int, seed: int, vol: float) -> list[Candle]:
    rng = random.Random(seed)  # noqa: S311
    price = 100.0
    candles = warmup(START)
    for i in range(START, count):
        close = price * (1 + rng.gauss(0, vol))
        high = max(price, close) * (1 + abs(rng.gauss(0, vol / 3)))
        low = min(price, close) * (1 - abs(rng.gauss(0, vol / 3)))
        candles.append(mk(i, *(str(D(x).quantize(CENT)) for x in (price, high, low, close))))
        price = close
    return candles


def trending_window(seed: int, *, drift: float, vol: float, spike_every: int, wave: float) -> list[Candle]:
    """400 velas sinteticas: tendencia de alta, ondas e barras 'elefante' periodicas com volume forte."""
    rng = random.Random(seed)  # noqa: S311
    price = 100.0
    candles: list[Candle] = []
    for i in range(400):
        close = price * (1 + rng.gauss(drift + wave * math.sin(i / 9.0), vol))
        volume = 1000.0 * (1 + abs(rng.gauss(0, 0.3)))
        high = max(price, close) * (1 + abs(rng.gauss(0, vol / 3)))
        low = min(price, close) * (1 - abs(rng.gauss(0, vol / 3)))
        if spike_every and i % spike_every == spike_every - 1:
            close, volume = price * (1 + 4 * vol), volume * 4.5
            high, low = close * 1.001, price * 0.999
        prices = (str(D(x).quantize(CENT)) for x in (price, high, low, close))
        candles.append(mk(i, *prices, str(D(volume).quantize(CENT))))
        price = close
    return candles


def levels(candle: Candle) -> tuple[str, str, str]:
    close = candle.close
    return str(close), str((close * D("0.985")).quantize(CENT)), str((close * D("1.04")).quantize(CENT))


async def run_runner(
    candles: list[Candle],
    start: int,
    *,
    strategies: Sequence[Strategy] | None = None,
    indexes: set[int] | None = None,
    scripted_levels: Callable[[Candle], tuple[str, str, str]] | None = None,
    history_window: int = 60,
) -> Harness:
    """Alimenta o runner vela a vela, como o motor do backtest: o book fecha na vela e abre na seguinte."""
    close = [candles[start].close]
    h = await Harness(
        strategy_indexes=indexes,
        strategies=strategies,
        history=candles[:start],
        # o book do gateway tem ask inutilizavel (entradas limit so enchem pela vela) e bid sem cruzar o stop
        # (stops disparam pela vela); no fechamento o bid valida o `raise_stop`; na abertura seguinte executam
        # as vendas a mercado e os gaps, ao preco de abertura, como no backtest. Cotacao do gate = fechamento.
        cfg=config(
            risk=RISK, history_window=history_window, max_book_age_seconds=10**6, max_stop_blind_seconds=10**6
        ),
        quotes=lambda _: (close[0], close[0]),
    ).start()
    if scripted_levels is not None:
        h.strategy.levels = scripted_levels
    for candle, following in pairwise([*candles[start:], None]):
        close[0] = candle.close
        h.clock.set(candle.close_time)
        stops = [o.stop for o in h.robot.gateway.open_protective_state()]
        bid = max([candle.close, *(stop + CENT for stop in stops)])
        await h.robot.on_book(make_book(str(bid), str(bid * 2)))
        await h.robot.on_candle(candle)
        if following is not None:
            await h.robot.on_book(make_book(str(following.open), str(following.open * 2)))
    return h


def trade_rows(trades: Sequence[Trade]) -> list[dict[str, object]]:
    return [t.model_dump(include=TRADE_KEYS) for t in trades]


async def run_both(
    seed: int, vol: float
) -> tuple[Harness, list[tuple[object, str]], list[tuple[object, str]], list[Trade]]:
    candles = random_walk(200, seed, vol)
    indexes = set(range(START, 199, 5))
    result = await BacktestEngine(
        [Scripted(indexes, levels)],
        settings(history_window=60, slippage_bps=D(0), risk=RISK),
        clock=lambda: FIXED_NOW,
    ).run(candles, start=START)
    h = await run_runner(candles, START, indexes=indexes, scripted_levels=levels)
    expected = [(d.at, d.code) for d in result.decisions if d.code != "entry_expired"]
    actual = [(d.decided_at, d.code) for d in h.journal.decisions if d.strategy == "scripted"]
    return h, expected, actual, list(result.trades)


@pytest.mark.parametrize(("seed", "vol"), [(1, 0.007), (2, 0.007), (3, 0.012), (4, 0.012), (5, 0.009)])
async def test_same_candles_give_the_same_decisions_and_trades_as_the_backtest(seed: int, vol: float) -> None:
    h, expected, actual, trades = await run_both(seed, vol)

    assert any(code == "approved" for _, code in expected), "o cenario precisa gerar entradas"
    assert actual == expected
    assert trade_rows(h.journal.trades) == trade_rows(trades)


# janelas sinteticas ja escolhidas (busca offline) para que as estrategias REAIS emitam varios sinais
REAL_WINDOWS = {
    "elephant": (ElephantBarBreakout, dict(seed=2, drift=0.0008, vol=0.006, spike_every=31, wave=0.0)),
    "ema": (EmaCrossTrend, dict(seed=32, drift=0.0006, vol=0.005, spike_every=23, wave=0.003)),
}


@pytest.mark.parametrize("name", ["elephant", "ema", "both"])
async def test_real_strategies_give_the_same_decisions_and_trades_as_the_backtest(name: str) -> None:
    if name == "both":
        window = dict(seed=4, drift=0.0006, vol=0.005, spike_every=23, wave=0.003)
        factories: list[Callable[[], Strategy]] = [ElephantBarBreakout, EmaCrossTrend]
    else:
        factory, window = REAL_WINDOWS[name]
        factories = [factory]
    candles = trending_window(**window)  # type: ignore[arg-type]
    start = 100
    result = await BacktestEngine(
        [make() for make in factories],
        settings(history_window=120, slippage_bps=D(0), risk=RISK),
        clock=lambda: FIXED_NOW,
    ).run(candles, start=start)

    h = await run_runner(candles, start, strategies=[make() for make in factories], history_window=120)

    approved = [d for d in result.decisions if d.code == "approved"]
    assert len(approved) >= 3 and len(result.trades) >= 2, "a janela precisa gerar sinais e operacoes"
    expected = [(d.at, d.code) for d in result.decisions if d.code != "entry_expired"]
    actual = [
        (d.decided_at, d.code)
        for d in h.journal.decisions
        if d.strategy is not None and d.code != "entry_expired"
    ]
    assert actual == expected
    assert trade_rows(h.journal.trades) == trade_rows(result.trades)
