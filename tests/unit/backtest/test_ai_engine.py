from engine.application.ai import AnalystOpinion, AnalystRequest
from engine.application.backtest import BacktestEngine, DecisionAction, summarize_avoided
from tests.unit.ai.support import opinion
from tests.unit.backtest.engine_helpers import (
    ENTRY_CANDLE,
    FIXED_NOW,
    Scripted,
    fixed_levels,
    flat_then,
    run,
    settings,
)

WIN = [ENTRY_CANDLE, ("100.5", "112", "100", "111.5"), ("111.5", "120", "111", "119")]


class Replay:
    def __init__(self, result: AnalystOpinion | Exception | None) -> None:
        self.result = result
        self.calls = 0

    async def analyze(self, request: AnalystRequest, *, decision_key: str = "") -> AnalystOpinion | None:
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


async def run_with(analyst: Replay | None, **kw: object):  # type: ignore[no-untyped-def]
    engine = BacktestEngine(
        [Scripted({29}, fixed_levels)],
        settings(),
        clock=lambda: FIXED_NOW,
        analyst=analyst,
        **kw,  # type: ignore[arg-type]
    )
    return await engine.run(flat_then(WIN))


async def test_default_backtest_has_no_ai_and_is_identical_to_an_explicit_none() -> None:
    plain = await run(flat_then(WIN))
    explicit = await run_with(None)
    assert plain.model_dump_json() == explicit.model_dump_json()


async def test_replay_veto_becomes_an_avoided_and_simulated_trade() -> None:
    analyst = Replay(opinion("veto", 0.9, "exaustao"))
    result = await run_with(analyst)

    assert analyst.calls == 1 and result.trades == ()
    [decision] = [d for d in result.decisions if d.code == "ai_veto"]
    assert decision.action is DecisionAction.SKIPPED and decision.hypothetical is not None
    assert "exaustao" in decision.message
    avoided = summarize_avoided(result.decisions)
    assert [(g.code, g.kind) for g in avoided.by_code] == [("ai_veto", "ai")]


async def test_replay_approval_trades_like_without_ai() -> None:
    baseline = await run(flat_then(WIN))
    approved = await run_with(Replay(opinion("approve", 0.7)))
    assert [t.model_dump() for t in approved.trades] == [t.model_dump() for t in baseline.trades]
    assert any((d.context or {}).get("ai_opinion") for d in approved.decisions)


async def test_low_confidence_from_ai_blocks_when_below_minimum() -> None:
    result = await run_with(Replay(opinion("approve", 0.1)), ai_min_confidence=0.5)
    assert [d.code for d in result.decisions if d.code.startswith("ai_")] == ["ai_low_confidence"]


async def test_failing_analyst_degrades_to_no_ai() -> None:
    baseline = await run(flat_then(WIN))
    result = await run_with(Replay(RuntimeError("x")))
    assert [t.model_dump() for t in result.trades] == [t.model_dump() for t in baseline.trades]
