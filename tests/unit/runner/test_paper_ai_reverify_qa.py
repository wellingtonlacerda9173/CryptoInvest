"""QA (reverificacao Fase 6): paradas e corridas com a IA lenta; a revalidacao pos-IA descarta o velho."""

import asyncio
from datetime import timedelta
from typing import Any

from engine.application.ai import AnalystOpinion, AnalystRequest
from engine.application.risk import EventCalendar, EventWindow
from engine.domain.risk import RiskConfig
from engine.runner.paper_robot import BotState
from tests.unit.ai.support import opinion
from tests.unit.runner.fakes import SYMBOL, Harness, candle_at, config, make_book, warmup
from tests.unit.runner.test_paper_ai import SIGNAL_CANDLE, ai_config

ETH = "ETH/USDT"


class GatedAnalyst:
    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.calls = 0
        self.entered = asyncio.Event()
        self.cond = asyncio.Condition()

    async def wait_calls(self, n: int) -> None:
        async with self.cond:
            await asyncio.wait_for(self.cond.wait_for(lambda: self.calls >= n), 3)

    async def analyze(self, request: AnalystRequest, *, decision_key: str = "") -> AnalystOpinion | None:
        async with self.cond:
            self.calls += 1
            self.cond.notify_all()
        self.entered.set()
        await self.release.wait()
        return opinion("approve", 0.9, "ok")


def _eth(c: Any) -> Any:
    return c.model_copy(update={"symbol": ETH})


async def _two_pairs(max_positions: int, analyst: GatedAnalyst) -> Harness:
    history = warmup() + [_eth(c) for c in warmup()]
    cfg = config(symbols=(SYMBOL, ETH), ai=ai_config().ai, risk=RiskConfig(max_open_positions=max_positions))
    h = Harness(cfg=cfg, analyst=analyst, history=history)
    await h.start()
    return h


async def test_two_pairs_with_a_slow_ai_never_exceed_the_position_limit() -> None:
    analyst = GatedAnalyst()
    h = await _two_pairs(1, analyst)
    btc = candle_at(29, *SIGNAL_CANDLE)
    eth = _eth(btc)
    h.clock.set(btc.close_time)
    await h.robot.on_book(make_book("99.99", "100.01"))
    await h.robot.on_book(make_book("99.99", "100.01", ETH))
    feeders = [asyncio.create_task(h.robot.on_candle(btc)), asyncio.create_task(h.robot.on_candle(eth))]
    await analyst.wait_calls(2)
    assert not h.robot._lock.locked()
    analyst.release.set()
    await asyncio.wait_for(asyncio.gather(*feeders), 3)

    entered = [d for d in h.journal.decisions if d.traded]
    skipped = [d for d in h.journal.decisions if d.code == "max_open_positions" or "limite" in d.reason]
    assert len(entered) == 1, [(d.code, d.symbol) for d in h.journal.decisions]
    assert len(h.robot._pending) + len(h.robot._live) == 1
    assert len(skipped) == 1


async def test_same_pair_two_candles_slow_ai_does_not_duplicate_the_entry() -> None:
    analyst = GatedAnalyst()
    h = await Harness(cfg=ai_config(), analyst=analyst).start()
    h.strategy.at = {29, 30}
    c29 = candle_at(29, *SIGNAL_CANDLE)
    c30 = candle_at(30, *SIGNAL_CANDLE)
    h.clock.set(c29.close_time)
    await h.tick()
    t1 = asyncio.create_task(h.robot.on_candle(c29))
    await analyst.wait_calls(1)
    h.clock.set(c30.close_time)
    await h.tick()
    t2 = asyncio.create_task(h.robot.on_candle(c30))
    await analyst.wait_calls(2)
    analyst.release.set()
    await asyncio.wait_for(asyncio.gather(t1, t2), 3)
    assert len([d for d in h.journal.decisions if d.traded]) == 1
    assert len(h.robot._pending) + len(h.robot._live) == 1


async def test_stop_requested_before_collection_places_nothing_and_leaves_the_lock_free() -> None:
    analyst = GatedAnalyst()
    analyst.release.set()
    h = await Harness(cfg=ai_config(), analyst=analyst).start()
    h.robot.request_stop("antes")
    await asyncio.wait_for(h.close(candle_at(29, *SIGNAL_CANDLE)), 3)
    assert h.journal.orders == {} and not h.robot._pending and not h.robot._lock.locked()
    await asyncio.wait_for(h.robot.shutdown("antes"), 3)


async def test_real_shutdown_while_ai_waits_then_answer_arrives_places_nothing() -> None:
    analyst = GatedAnalyst()
    h = await Harness(cfg=ai_config(), analyst=analyst).start()
    candle = candle_at(29, *SIGNAL_CANDLE)
    h.clock.set(candle.close_time)
    await h.tick()
    feeder = asyncio.create_task(h.robot.on_candle(candle))
    await asyncio.wait_for(analyst.entered.wait(), 2)
    h.robot.request_stop("sigterm")
    await asyncio.wait_for(h.robot.shutdown("sigterm"), 3)
    analyst.release.set()
    await asyncio.wait_for(feeder, 3)
    assert h.journal.orders == {} and not h.robot._pending and not h.robot._lock.locked()
    assert not any(d.traded for d in h.journal.decisions)


async def test_state_not_running_after_the_ai_drops_the_entry() -> None:
    analyst = GatedAnalyst()
    h = await Harness(cfg=ai_config(), analyst=analyst).start()
    candle = candle_at(29, *SIGNAL_CANDLE)
    h.clock.set(candle.close_time)
    await h.tick()
    feeder = asyncio.create_task(h.robot.on_candle(candle))
    await asyncio.wait_for(analyst.entered.wait(), 2)
    h.robot._bot_state = BotState.PAUSED
    analyst.release.set()
    await asyncio.wait_for(feeder, 3)
    assert h.journal.orders == {} and not h.robot._pending


async def test_cancel_during_ai_wait_never_touches_the_lock_and_places_nothing() -> None:
    analyst = GatedAnalyst()
    h = await Harness(cfg=ai_config(), analyst=analyst).start()
    candle = candle_at(29, *SIGNAL_CANDLE)
    h.clock.set(candle.close_time)
    await h.tick()
    feeder = asyncio.create_task(h.robot.on_candle(candle))
    await asyncio.wait_for(analyst.entered.wait(), 2)
    async with h.robot._lock:  # o lock fica preso por quem cancela; o cancelamento nao pode pedir o lock
        feeder.cancel()
        await asyncio.wait_for(asyncio.gather(feeder, return_exceptions=True), 2)
    assert feeder.cancelled() and h.journal.orders == {}


async def test_stop_during_the_second_gate_never_leaves_a_pending_entry_after_shutdown() -> None:
    analyst = GatedAnalyst()
    analyst.release.set()
    h = await Harness(cfg=ai_config(), analyst=analyst).start()
    original = h.robot._quotes
    candle = candle_at(29, *SIGNAL_CANDLE)
    h.clock.set(candle.close_time)
    await h.tick()
    # primeiro gate (sob lock, sem IA ainda): so a partir do 2o gate o stop e disparado
    calls = {"n": 0}

    def hook(symbol: str) -> Any:
        calls["n"] += 1
        if calls["n"] >= 2:
            h.robot.request_stop("durante o 2o gate")
        return original(symbol)

    h.robot._quotes = hook  # type: ignore[method-assign]
    await asyncio.wait_for(h.robot.on_candle(candle), 3)
    await asyncio.wait_for(h.robot.shutdown("x"), 3)
    assert calls["n"] >= 2
    assert not h.robot._pending
    assert not h.robot._lock.locked()


async def test_stop_set_before_the_ai_phase_never_calls_the_analyst() -> None:
    analyst = GatedAnalyst()
    analyst.release.set()
    h = await Harness(cfg=ai_config(), analyst=analyst).start()
    original = h.robot._quotes
    candle = candle_at(29, *SIGNAL_CANDLE)
    h.clock.set(candle.close_time)
    await h.tick()

    def hook(symbol: str) -> Any:
        h.robot.request_stop("antes da IA")
        return original(symbol)

    h.robot._quotes = hook  # type: ignore[method-assign]
    await asyncio.wait_for(h.robot.on_candle(candle), 3)
    assert analyst.calls == 0
    assert h.journal.orders == {} and not h.robot._pending and not h.robot._lock.locked()


async def test_event_window_starting_during_the_ai_wait_rejects_at_the_second_gate() -> None:
    candle = candle_at(29, *SIGNAL_CANDLE)
    window = EventWindow(
        start=candle.close_time + timedelta(minutes=40),
        end=candle.close_time + timedelta(hours=2),
        label="FOMC",
    )
    calendar = EventCalendar(
        windows=(window,),
        loaded_at=candle.close_time - timedelta(days=1),
        valid_until=candle.close_time + timedelta(days=30),
    )
    analyst = GatedAnalyst()
    h = await Harness(cfg=ai_config(), analyst=analyst, calendar=calendar).start()
    h.clock.set(candle.close_time)
    await h.tick()
    feeder = asyncio.create_task(h.robot.on_candle(candle))
    await asyncio.wait_for(analyst.entered.wait(), 2)
    h.clock.set(candle.close_time + timedelta(minutes=20))
    await h.robot.on_book(make_book("99.99", "100.01"))
    analyst.release.set()
    await asyncio.wait_for(feeder, 3)
    assert h.journal.orders == {} and not h.robot._pending
    assert [d.code for d in h.journal.decisions if d.symbol == SYMBOL][-1] == "event_window"
