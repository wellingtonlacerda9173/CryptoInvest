import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from engine.application.backtest import (
    BacktestEngine,
    BacktestResult,
    BacktestSettings,
    DecisionAction,
    ReportMeta,
    compute_metrics,
    summarize_avoided,
    write_backtest_report,
)
from engine.application.regime import Regime, RegimeTimeline
from engine.application.selector import (
    SELECTOR_IDLE,
    SELECTOR_VETO,
    SelectorConfig,
    SelectorSettings,
    TradeSample,
)
from engine.application.strategies import (
    ElephantBarBreakout,
    EmaCrossTrend,
    Strategy,
    StrategyContext,
    StrategySignal,
)
from engine.domain.candle import Candle
from engine.domain.signal import Signal
from engine.domain.types import Timeframe
from tests.synthetic import synthetic_candles

START = datetime(2025, 1, 1, tzinfo=UTC)
NOW = datetime(2030, 1, 1, tzinfo=UTC)
CONFLUENCES = (
    "volume_forte",
    "preco_acima_da_ema_de_tendencia",
    "rsi_em_zona_de_forca",
    "fechamento_acima_da_maxima_recente",
)
EMA, ELEPHANT = EmaCrossTrend().name, ElephantBarBreakout().name
OPERABLE = (Regime.TENDENCIA_ALTA, Regime.TENDENCIA_BAIXA, Regime.LATERAL)
# sha256 gerados ANTES da integracao do seletor (mesmo backtest, seletor inexistente)
BASELINE = {
    "trades.csv": "6b2bd35c444fe37e42d884349ba43c5a880e1f6f47f78209957736846238e1d9",
    "equity.csv": "8188e3e3add9449844fa638a91b6f65953196ef1248682f43ac748ed6c2eaa9b",
    "decisions.csv": "fddd3405963e51d6d8614d49baaec309096d1fe73e71c61ff7d2f9fb16bab9dc",
}


class Scripted:
    """Sinaliza nas velas indicadas (entrada no fechamento, stop 1%, alvo 3,5%)."""

    def __init__(self, name: str, at: set[int]) -> None:
        self.name = name
        self.at = at

    def evaluate(self, candles: Sequence[Candle], context: StrategyContext) -> StrategySignal | None:
        last = candles[-1]
        if int((last.open_time - START) / timedelta(hours=1)) not in self.at:
            return None
        signal = Signal(
            symbol=last.symbol,
            timeframe=last.timeframe,
            created_at=last.close_time,
            strategy=self.name,
            entry=last.close,
            stop_loss=(last.close * Decimal("0.99")).quantize(Decimal("0.0001")),
            take_profit=(last.close * Decimal("1.035")).quantize(Decimal("0.0001")),
        )
        return StrategySignal(
            signal=signal, confidence=0.8, rationale="teste", triggers=(), confluences=CONFLUENCES
        )


def trending_indices(data: list[Candle], lo: int, hi: int, every: int = 100) -> set[int]:
    timeline = RegimeTimeline.from_candles(data)
    picked: set[int] = set()
    last = -every
    for i in range(lo, hi):
        reading = timeline.at(data[i].close_time)
        if reading is not None and reading.operable and i - last >= every:
            picked.add(i)
            last = i
    return picked


def candles(count: int = 24 * 60) -> list[Candle]:
    return synthetic_candles("BTC/USDT", Timeframe.H1, START, count)


def history(strategy: str, mean_r: float, closed: datetime = START - timedelta(days=1)) -> list[TradeSample]:
    out: list[TradeSample] = []
    for regime in OPERABLE:
        for k in range(40):
            end = closed - timedelta(hours=k)
            out.append(
                TradeSample(
                    strategy=strategy,
                    symbol="BTC/USDT",
                    regime=regime,
                    opened_at=end - timedelta(hours=2),
                    closed_at=end,
                    r_multiple=mean_r + (0.5 if k % 2 else -0.5),
                )
            )
    return out


def on(**kw: object) -> SelectorSettings:
    return SelectorSettings(enabled=True, selector=SelectorConfig(**kw))  # type: ignore[arg-type]


async def run(
    data: list[Candle],
    selector: SelectorSettings | None = None,
    samples: list[TradeSample] | None = None,
    strategies: Sequence[Strategy] | None = None,
) -> BacktestResult:
    engine = BacktestEngine(
        strategies or [ElephantBarBreakout(), EmaCrossTrend()],
        BacktestSettings(),
        clock=lambda: NOW,
        selector=selector,
        selector_history=samples or (),
    )
    return await engine.run(data)


def csv_hashes(result: BacktestResult, out: Path) -> dict[str, str]:
    meta = ReportMeta(name="x", git_sha=None, git_dirty=None, data_source="s", data_notes=(), parameters={})
    write_backtest_report(out, result, compute_metrics(result), meta)
    return {name: hashlib.sha256((out / name).read_bytes()).hexdigest() for name in BASELINE}


@pytest.mark.parametrize("selector", [None, SelectorSettings()])
async def test_disabled_selector_keeps_reports_identical(
    selector: SelectorSettings | None, tmp_path: Path
) -> None:
    result = await run(candles(), selector, history(EMA, 1.0))
    assert csv_hashes(result, tmp_path) == BASELINE
    assert all(d.context is None for d in result.decisions)


async def test_without_scores_never_trades_and_explains() -> None:
    result = await run(candles(), on())
    assert not result.round_trips
    idle = [d for d in result.decisions if d.code == SELECTOR_IDLE]
    assert idle
    assert all(d.action is DecisionAction.SKIPPED and d.message.startswith("Não operar") for d in idle)
    ctx = idle[0].context or {}
    assert {"regime", "scores", "ai_opinion"} <= ctx.keys()
    assert ctx["ai_opinion"] == ""


def pair(data: list[Candle], lo: int = 150, hi: int = 1400) -> list[Strategy]:
    at = trending_indices(data, lo, hi)
    assert len(at) >= 5
    return [Scripted("alfa", at), Scripted("beta", at)]


async def test_only_chosen_strategy_reaches_the_gate_and_vetoed_count_as_avoided() -> None:
    data = candles()
    result = await run(data, on(), history("alfa", 1.0), pair(data))
    assert result.round_trips
    assert {t.strategy for t in result.round_trips} == {"alfa"}
    vetoes = [d for d in result.decisions if d.code == SELECTOR_VETO]
    assert vetoes
    assert {d.strategy for d in vetoes} == {"beta"}
    assert all(d.hypothetical is not None and d.context for d in vetoes)
    traded = [d for d in result.decisions if d.action is DecisionAction.TRADED]
    assert traded
    assert all(d.strategy == "alfa" and d.context for d in traded)
    assert all(d.context["scores"] and d.context["regime"] for d in traded if d.context)
    avoided = summarize_avoided(result.decisions)
    assert any(g.code == SELECTOR_VETO and g.kind == "selector" for g in avoided.by_code)
    assert avoided.risk_vetoes_net_effect == 0


def comparable(result: BacktestResult, before: datetime) -> list[tuple[object, ...]]:
    return [
        (d.at, d.strategy, d.action, d.code, d.message, d.entry, repr(d.context))
        for d in result.decisions
        if d.at < before
    ]


async def test_no_leakage_from_future_candles_or_future_trades() -> None:
    full = candles()
    cut = 24 * 40
    cutoff = full[cut].open_time
    shaken = [
        c.model_copy(update={"open": c.open * 2, "high": c.high * 3, "low": c.low / 2, "close": c.close * 2})
        for c in full[cut:]
    ]
    strategies = pair(full, 150, cut - 5)
    base_samples = history("alfa", 1.0)
    poison = history("alfa", -9.0, closed=cutoff + timedelta(days=3))
    reference = await run(full, on(), base_samples, strategies)
    changed_candles = await run([*full[:cut], *shaken], on(), base_samples, strategies)
    changed_trades = await run(full, on(), [*base_samples, *poison], strategies)
    expected = comparable(reference, cutoff)
    assert any(row[3] == "approved" for row in expected)
    assert comparable(changed_candles, cutoff) == expected
    assert comparable(changed_trades, cutoff) == expected


async def test_scores_only_use_trades_closed_before_each_decision() -> None:
    data = candles()
    late = history("alfa", 1.0, closed=START + timedelta(days=50))
    result = await run(data, on(), late, pair(data))
    first_known = START + timedelta(days=50) - timedelta(hours=40)
    assert result.round_trips
    assert all(t.opened_at >= first_known for t in result.round_trips)
