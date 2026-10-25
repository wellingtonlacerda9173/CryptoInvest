import json
from typing import Any

from engine.application.ai import AnalystOpinion, AnalystRequest
from engine.application.trading_config.document import AiConfig
from tests.unit.ai.support import opinion
from tests.unit.runner.fakes import Harness, candle_at, config

SIGNAL_CANDLE = ("100", "100.5", "99.5", "100")


class FakeAnalyst:
    def __init__(self, result: AnalystOpinion | Exception | None) -> None:
        self.result = result
        self.requests: list[AnalystRequest] = []
        self.keys: list[str] = []
        self.lock_held_during_call: list[bool] = []
        self.harness: Harness | None = None

    async def analyze(self, request: AnalystRequest, *, decision_key: str = "") -> AnalystOpinion | None:
        self.requests.append(request)
        self.keys.append(decision_key)
        if self.harness is not None:
            self.lock_held_during_call.append(self.harness.robot._lock.locked())
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def status(self) -> dict[str, Any]:
        return {"circuit_open": False, "calls_last_hour": 1}


def ai_config(enabled: bool = True, **kw: Any) -> Any:
    return config(ai=AiConfig(enabled=enabled), **kw)


async def run(analyst: FakeAnalyst | None, cfg: Any = None, **kw: Any) -> Harness:
    h = await Harness(cfg=cfg or ai_config(), analyst=analyst, **kw).start()
    if analyst is not None:
        analyst.harness = h
    await h.close(candle_at(29, *SIGNAL_CANDLE))
    return h


async def test_veto_records_ai_veto_with_the_reason_and_places_no_order() -> None:
    analyst = FakeAnalyst(opinion("veto", 0.9, "tendencia contraria"))
    h = await run(analyst)

    [decision] = h.journal.decisions
    assert decision.code == "ai_veto" and not decision.traded and h.journal.orders == {}
    assert "tendencia contraria" in decision.reason
    assert "Vetou" in (decision.insight or {})["ai_opinion"]
    assert (decision.insight or {})["ai_detail"]["untrusted"] is True
    assert decision.signal is not None and decision.strategy == "scripted"


async def test_approval_proceeds_and_the_opinion_goes_to_the_panel_field() -> None:
    h = await run(FakeAnalyst(opinion("approve", 0.6, "setup coerente")))

    [decision] = h.journal.decisions
    assert decision.code == "approved" and decision.traded and list(h.journal.orders)
    assert (decision.insight or {})["ai_opinion"].startswith("Aprovou (confiança 60%)")


async def test_approval_that_drops_confidence_below_the_minimum_becomes_ai_low_confidence() -> None:
    h = await run(FakeAnalyst(opinion("approve", 0.2, "fraco")), ai_config(ai_min_confidence=0.5))

    [decision] = h.journal.decisions
    assert decision.code == "ai_low_confidence" and not decision.traded and h.journal.orders == {}


async def test_ai_only_runs_after_the_gate_approved() -> None:
    analyst = FakeAnalyst(opinion("veto"))
    h = await Harness(cfg=ai_config(), analyst=analyst, quotes=lambda _: None).start()
    await h.close(candle_at(29, *SIGNAL_CANDLE))

    assert analyst.requests == []
    assert [d.code for d in h.journal.decisions] == ["no_quote"]


async def test_disabled_ai_never_calls_the_analyst_and_matches_a_run_without_it() -> None:
    analyst = FakeAnalyst(opinion("veto"))
    with_fake = await run(analyst, cfg=ai_config(enabled=False))
    baseline = await run(None, cfg=config())

    assert analyst.requests == []
    assert [(d.code, d.traded, d.insight) for d in with_fake.journal.decisions] == [
        (d.code, d.traded, d.insight) for d in baseline.journal.decisions
    ]
    assert with_fake.robot.config.config_hash() == baseline.robot.config.config_hash()


async def test_no_opinion_behaves_exactly_like_without_ai() -> None:
    h = await run(FakeAnalyst(None))
    baseline = await run(None, cfg=config())
    assert [(d.code, d.traded) for d in h.journal.decisions] == [
        (d.code, d.traded) for d in baseline.journal.decisions
    ]
    assert list(h.journal.orders) == list(baseline.journal.orders)


async def test_analyst_failure_never_stops_the_robot() -> None:
    h = await run(FakeAnalyst(RuntimeError("quebrou")))
    [decision] = h.journal.decisions
    assert decision.code == "approved" and decision.traded


async def test_request_has_only_market_and_signal_data() -> None:
    analyst = FakeAnalyst(opinion("approve"))
    h = await run(analyst)
    [request] = analyst.requests
    text = json.dumps(request.model_dump())
    for private in (str(h.cfg.capital), h.journal.run_id, "equity", "balance"):
        assert private not in text
    assert request.signal.strategy == "scripted" and request.symbol == "BTC/USDT"
    assert analyst.keys[0].startswith("scripted:BTC/USDT:")


async def test_robot_does_not_hold_the_lock_while_waiting_for_the_ai() -> None:
    analyst = FakeAnalyst(opinion("approve"))
    h = await run(analyst)
    assert analyst.lock_held_during_call == [False]
    assert not h.robot._lock.locked()


async def test_gate_runs_again_after_the_ai_so_state_changes_are_respected() -> None:
    analyst = FakeAnalyst(opinion("approve"))
    h = await Harness(cfg=ai_config(), analyst=analyst).start()
    analyst.harness = h
    original = analyst.analyze

    async def blind_during_call(request: AnalystRequest, *, decision_key: str = "") -> AnalystOpinion | None:
        h.robot._blind = True
        return await original(request, decision_key=decision_key)

    h.robot.analyst = type("A", (), {"analyze": staticmethod(blind_during_call)})()  # type: ignore[assignment]
    await h.close(candle_at(29, *SIGNAL_CANDLE))
    [decision] = h.journal.decisions
    assert decision.code == "stops_blind" and h.journal.orders == {}


async def test_status_exposes_the_ai_state() -> None:
    h = await run(FakeAnalyst(opinion("approve")))
    payload = h.robot.status_payload(h.clock.now())["ai"]
    assert payload["enabled"] is True and payload["circuit_open"] is False


class FakeLessons:
    def __init__(self, lessons: list[Any] | Exception) -> None:
        self.lessons = lessons
        self.asked = 0

    async def approved(self, limit: int) -> list[Any]:
        self.asked += 1
        if isinstance(self.lessons, Exception):
            raise self.lessons
        return self.lessons


def stored(status: str, text: str) -> Any:
    from datetime import UTC, datetime

    from engine.application.ai.lessons import LessonReport, Observation, StoredLesson

    at = datetime(2026, 10, 24, tzinfo=UTC)
    return StoredLesson(
        id="a" * 32,
        created_at=at,
        period_start=at,
        period_end=at,
        source="ai",
        prompt_version="lessons-v1",
        summary={},
        report=LessonReport(
            summary="s", observations=(Observation(text=text, evidence="4 de 9 trades", confidence="low"),)
        ),
        status=status,
    )


async def test_only_approved_lessons_enter_the_request_and_the_decision_is_unchanged() -> None:
    analyst = FakeAnalyst(opinion("veto", 0.9, "contra"))
    source = FakeLessons([stored("approved", "licao boa"), stored("pending", "licao pendente")])
    h = await Harness(cfg=ai_config(), analyst=analyst, lesson_source=source).start()
    await h.close(candle_at(29, *SIGNAL_CANDLE))

    [request] = analyst.requests
    assert len(request.lessons) == 1 and "licao boa" in request.lessons[0]
    assert [d.code for d in h.journal.decisions] == ["ai_veto"]


async def test_lessons_failure_or_absence_behaves_exactly_like_before() -> None:
    for source in (None, FakeLessons(RuntimeError("banco fora")), FakeLessons([stored("rejected", "x")])):
        analyst = FakeAnalyst(opinion("approve", 0.6))
        h = await Harness(cfg=ai_config(), analyst=analyst, lesson_source=source).start()
        await h.close(candle_at(29, *SIGNAL_CANDLE))
        assert analyst.requests[0].lessons == ()
        assert [d.code for d in h.journal.decisions] == ["approved"]
