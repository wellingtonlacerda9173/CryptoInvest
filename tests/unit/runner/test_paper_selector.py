from collections.abc import Sequence
from datetime import datetime, timedelta

import pytest

from engine.application.regime import Regime, RegimeReading
from engine.application.selector import (
    SELECTOR_IDLE,
    SELECTOR_NO_SCORES,
    ScoreKey,
    SelectorSettings,
    StrategyScore,
    TradeSample,
    build_scores,
)
from engine.infrastructure.persistence.score_repository import Origin
from engine.runner import paper_robot
from tests.unit.backtest.engine_helpers import T0, Scripted
from tests.unit.runner.fakes import Harness, candle_at, config

SIGNAL_CANDLE = ("100", "100.5", "99.5", "100")
SYMBOL = "BTC/USDT"


class Other(Scripted):
    name = "outra"


class FakeScoreStore:
    def __init__(
        self, scores: dict[ScoreKey, StrategyScore] | None = None, journal: Sequence[TradeSample] = ()
    ) -> None:
        self.scores = scores or {}
        self.journal = list(journal)
        self.published: list[tuple[list[StrategyScore], str, Origin, datetime, str | None]] = []

    async def latest_scores(self, timeframe: str, symbols: Sequence[str]) -> dict[ScoreKey, StrategyScore]:
        return self.scores

    async def journal_samples(self, symbols: Sequence[str]) -> list[TradeSample]:
        return self.journal

    async def publish(
        self,
        scores: Sequence[StrategyScore],
        *,
        timeframe: str,
        origin: Origin,
        calculated_at: datetime,
        run_id: str | None = None,
    ) -> str:
        self.published.append((list(scores), timeframe, origin, calculated_at, run_id))
        return "snap"


def samples(strategy: str, mean_r: float, count: int = 40) -> list[TradeSample]:
    out: list[TradeSample] = []
    for k in range(count):
        end = T0 - timedelta(hours=k + 1)
        out.append(
            TradeSample(
                strategy=strategy,
                symbol=SYMBOL,
                regime=Regime.TENDENCIA_ALTA,
                opened_at=end - timedelta(hours=2),
                closed_at=end,
                r_multiple=mean_r + (0.5 if k % 2 else -0.5),
            )
        )
    return out


def scores_for(*pairs: tuple[str, float]) -> dict[ScoreKey, StrategyScore]:
    pool = [s for name, mean in pairs for s in samples(name, mean)]
    return build_scores(pool, T0 + timedelta(hours=29), SelectorSettings().selector)


def force_regime(monkeypatch: pytest.MonkeyPatch, regime: Regime, operable: bool = True) -> None:
    def reading(window: Sequence[object], config: object = None) -> RegimeReading:
        return RegimeReading(
            at=T0,
            regime=regime,
            raw_regime=regime,
            range_defined=False,
            operable=operable,
            reason="teste",
            bars_in_regime=5,
        )

    monkeypatch.setattr(paper_robot, "classify_regime", reading)


def selector_config(**kw: object):  # type: ignore[no-untyped-def]
    return config(selector=SelectorSettings(enabled=True), **kw)


async def test_selector_without_scores_does_not_trade_and_says_why() -> None:
    scripted = Scripted({29})
    h = await Harness(cfg=selector_config(), strategies=[scripted], score_store=FakeScoreStore()).start()
    await h.close(candle_at(29, *SIGNAL_CANDLE))

    [decision] = h.journal.decisions
    assert decision.code == SELECTOR_NO_SCORES and not decision.traded
    assert "sem scores" in decision.reason and h.journal.orders == {}
    assert scripted.seen == []


async def test_selector_runs_only_the_chosen_strategy(monkeypatch: pytest.MonkeyPatch) -> None:
    force_regime(monkeypatch, Regime.TENDENCIA_ALTA)
    chosen, ignored = Scripted({29}), Other({29})
    store = FakeScoreStore(scores_for(("scripted", 1.0), ("outra", 0.0)))
    h = await Harness(cfg=selector_config(), strategies=[chosen, ignored], score_store=store).start()
    await h.close(candle_at(29, *SIGNAL_CANDLE))

    [decision] = h.journal.decisions
    assert decision.code == "approved" and decision.traded and decision.strategy == "scripted"
    assert ignored.seen == []
    insight = decision.insight or {}
    assert insight["regime"] == "tendência de alta" and insight["ai_opinion"] == ""
    assert {s["strategy"] for s in insight["scores"]} == {"scripted", "outra"}
    assert list(h.journal.orders)


async def test_regime_without_operation_records_a_portuguese_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    force_regime(monkeypatch, Regime.ALTA_VOLATILIDADE, operable=False)
    scripted = Scripted({29})
    store = FakeScoreStore(scores_for(("scripted", 1.0)))
    h = await Harness(cfg=selector_config(), strategies=[scripted], score_store=store).start()
    await h.close(candle_at(29, *SIGNAL_CANDLE))

    [decision] = h.journal.decisions
    assert decision.code == SELECTOR_IDLE and not decision.traded
    assert decision.reason.startswith("Não operar") and "alta volatilidade" in decision.reason
    assert (decision.insight or {})["regime_code"] == "alta_volatilidade"
    assert scripted.seen == [] and h.journal.orders == {}


async def test_no_eligible_strategy_in_the_regime_does_not_trade(monkeypatch: pytest.MonkeyPatch) -> None:
    force_regime(monkeypatch, Regime.TENDENCIA_BAIXA)
    store = FakeScoreStore(scores_for(("scripted", 1.0)))
    h = await Harness(cfg=selector_config(), strategies=[Scripted({29})], score_store=store).start()
    await h.close(candle_at(29, *SIGNAL_CANDLE))

    [decision] = h.journal.decisions
    assert decision.code == SELECTOR_IDLE and "nenhuma estratégia elegível" in decision.reason


async def test_disabled_selector_ignores_the_score_store() -> None:
    store = FakeScoreStore()
    h = await Harness(strategies=[Scripted({29})], score_store=store).start()
    await h.close(candle_at(29, *SIGNAL_CANDLE))

    assert h.journal.codes() == ["approved"]
    assert (h.journal.decisions[0].insight) is None


async def test_scores_are_recalculated_and_published_every_n_hours(monkeypatch: pytest.MonkeyPatch) -> None:
    force_regime(monkeypatch, Regime.TENDENCIA_ALTA)
    store = FakeScoreStore(scores_for(("scripted", 1.0)), journal=samples("scripted", 1.0, 20))
    h = await Harness(
        cfg=selector_config(),
        strategies=[Scripted(set())],
        score_store=store,
        backtest_samples=samples("scripted", 1.0, 20),
    ).start()
    h.clock.advance(seconds=3600)
    await h.robot.housekeeping()
    assert store.published == []

    h.clock.advance(seconds=6 * 3600)
    await h.robot.housekeeping()

    [(scores, timeframe, origin, _, run_id)] = store.published
    assert timeframe == "1h" and origin == "paper" and run_id == h.robot.run_id
    assert scores[0].trades == 40

    h.clock.advance(seconds=3600)
    await h.robot.housekeeping()
    assert len(store.published) == 1


async def test_recalculation_without_any_sample_keeps_the_published_scores() -> None:
    store = FakeScoreStore(scores_for(("scripted", 1.0)))
    h = await Harness(cfg=selector_config(), strategies=[Scripted(set())], score_store=store).start()
    h.clock.advance(seconds=7 * 3600)
    await h.robot.housekeeping()

    assert store.published == []
    assert h.robot._scores == store.scores
