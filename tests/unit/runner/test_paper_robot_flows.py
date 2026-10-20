"""Fluxos de gestao do paper: parcial, trailing, protecao de lucro, heartbeat e reconciliacao periodica."""

import asyncio
from collections.abc import AsyncIterator, Mapping
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from engine.domain.market import OrderBookSnapshot
from engine.domain.order import OrderStatus
from engine.runner.paper_config import RunPaths
from engine.runner.paper_control import read_status
from tests.unit.runner.fakes import (
    FakeFeed,
    Harness,
    SimulatedCrash,
    candle_at,
    make_book,
    warmup,
)
from tests.unit.runner.test_paper_robot import ENTRY_29, entered

D = Decimal
TARGET_CANDLE = ("100.5", "112", "100.4", "111.5")


async def test_partial_at_target_then_trailing_stop_exit_in_profit() -> None:
    h = await entered()
    await h.close(candle_at(31, *TARGET_CANDLE))

    [partial] = h.journal.trades
    assert partial.exit_reason.value == "take_profit"
    assert partial.exit_price == D(111)
    position = h.journal.positions["BTC/USDT"]
    assert position.partial_done
    assert position.stop == D(107)
    assert position.quantity == partial.quantity or position.quantity < position.initial_quantity
    [oco] = h.robot.gateway.open_protective_state()
    assert oco.stop == D(107)
    assert oco.quantity == position.quantity
    assert h.journal.orders[f"{ENTRY_29}:partial"].status is OrderStatus.FILLED

    await h.close(candle_at(32, "111.5", "111.6", "106", "106.5"), bid="107.5", ask="107.6")

    assert [t.exit_reason.value for t in h.journal.trades] == ["take_profit", "stop_loss"]
    assert h.journal.trades[-1].exit_price == D(107)
    assert h.journal.positions == {}
    assert h.robot.state.equity > D(10_000)
    assert len(h.robot.state.ledger) == 2


async def test_profit_protection_exit_is_sold_at_market_on_the_next_tick() -> None:
    h = await entered()
    await h.close(candle_at(31, "100.5", "108", "100.4", "104"))

    assert h.robot.live_symbols == ["BTC/USDT"]
    assert f"{ENTRY_29}:x1" in h.journal.orders
    assert h.journal.orders[f"{ENTRY_29}:partial"].status is OrderStatus.CANCELED
    h.clock.advance(seconds=1)
    await h.tick("103.9", "104.1")

    [trade] = h.journal.trades
    assert trade.exit_reason.value == "profit_protection"
    assert trade.exit_price == D("103.9")
    assert h.journal.positions == {}
    assert h.journal.orders[f"{ENTRY_29}:x1"].status is OrderStatus.FILLED


async def test_market_exit_without_a_fresh_book_waits_and_retries() -> None:
    h = await entered()
    candle = candle_at(31, "100.5", "108", "100.4", "104")
    h.clock.set(candle.close_time)
    await h.robot.on_candle(candle)

    assert h.robot.live_symbols == ["BTC/USDT"]
    assert not [o for o in h.journal.orders if ":x" in o]
    await h.tick("103.9", "104.1")
    assert f"{ENTRY_29}:x1" in h.journal.orders
    h.clock.advance(seconds=1)
    await h.tick("103.9", "104.1")

    assert h.journal.positions == {}


async def test_housekeeping_publishes_heartbeat_equity_and_status(tmp_path: Path) -> None:
    h = await Harness().start()
    h.robot.paths = RunPaths(tmp_path, "test-run")
    await h.tick()
    await h.robot.housekeeping()
    h.clock.advance(seconds=61)
    await h.tick()
    await h.robot.housekeeping()

    assert len(h.bus.of("heartbeat")) == 2
    assert h.bus.of("heartbeat")[0]["state"] == "RUNNING"
    assert len(h.journal.equity) >= 2
    status = read_status(h.robot.paths.status)  # type: ignore[union-attr]
    assert status is not None
    assert status["state"] == "RUNNING"
    assert status["open_positions"] == []


async def test_periodic_reconcile_is_clean_with_a_healthy_position() -> None:
    h = await entered()
    h.clock.advance(seconds=301)
    await h.tick()
    await h.robot.housekeeping()

    assert not h.robot.state.paused
    assert h.bus.of("alerts") == []


async def test_max_runtime_requests_a_graceful_stop() -> None:

    h = await Harness().start()
    h.robot.max_runtime = timedelta(minutes=10)
    h.clock.advance(seconds=601)
    await h.tick()
    await h.robot.housekeeping()

    assert h.robot.stop_event.is_set()


async def test_run_follows_the_feeds_and_stops_gracefully() -> None:

    candle = candle_at(29, "100", "100.5", "99.5", "100")
    feed = FakeFeed(candles=warmup(), books=[make_book("99.99", "100.01")], stream=[candle])
    h = await Harness(feed=feed).start()
    h.clock.set(candle.close_time)
    task = asyncio.create_task(h.robot.run())
    for _ in range(200):
        if "approved" in h.journal.codes():
            break
        await asyncio.sleep(0.02)
    h.robot.request_stop("fim do teste")
    await asyncio.wait_for(task, 5)
    await h.robot.shutdown()

    assert "approved" in h.journal.codes()
    assert h.robot.bot_state.value == "STOPPED"
    assert h.journal.orders["e2601020600BTC"].status is OrderStatus.CANCELED
    assert h.journal.controls[-1].action.value == "stop"


async def test_fatal_journal_failure_does_not_save_inconsistent_state() -> None:

    candles = [candle_at(29, "100", "100.5", "99.5", "100")]
    h = await Harness(feed=FakeFeed(candles=warmup(), stream=candles)).start()
    h.journal.crash_on = {"record_decision"}
    h.clock.set(candles[0].close_time)
    await h.tick()
    task = asyncio.create_task(h.robot.run())
    with pytest.raises(SimulatedCrash):
        await asyncio.wait_for(task, 5)
    saved = len(h.journal.gateway_states)
    await h.robot.shutdown()

    assert h.robot.failed
    assert len(h.journal.gateway_states) == saved


async def test_feed_failure_reconnects_with_backoff_and_keeps_going() -> None:

    class Flaky(FakeFeed):
        attempts = 0

        async def stream_order_book(self, symbol: str, depth: int = 10) -> AsyncIterator[OrderBookSnapshot]:
            Flaky.attempts += 1
            if Flaky.attempts == 1:
                raise ConnectionError("ws caiu")
            yield make_book("99.99", "100.01")
            await asyncio.sleep(30)

    h = await Harness(feed=Flaky(candles=warmup())).start()
    task = asyncio.create_task(h.robot.run())
    for _ in range(300):
        if h.robot.gateway.best_prices("BTC/USDT") is not None:
            break
        await asyncio.sleep(0.02)
    h.robot.request_stop("fim")
    await asyncio.wait_for(task, 5)

    assert Flaky.attempts >= 2
    assert h.robot.gateway.best_prices("BTC/USDT") == (D("99.99"), D("100.01"))


async def test_blind_with_open_position_escalates_after_the_configured_time() -> None:
    h = await entered()
    h.clock.advance(seconds=31)
    await h.robot.housekeeping()
    h.clock.advance(seconds=h.cfg.blind_alert_seconds + 1)
    await h.robot.housekeeping()

    kinds = [a["alert"] for a in h.bus.of("alerts")]
    assert kinds.count("stops_blind") == 1
    assert kinds.count("stops_blind_prolonged") == 1
    assert "prolongado" in h.journal.controls[-1].reason
    await h.robot.housekeeping()
    assert [a["alert"] for a in h.bus.of("alerts")].count("stops_blind_prolonged") == 1


async def test_event_bus_failure_never_stops_the_robot() -> None:

    class Broken:
        healthy = False

        async def publish(self, kind: str, payload: Mapping[str, Any]) -> None:
            raise RuntimeError("redis caiu")

        async def close(self) -> None:
            return None

    h = await Harness().start()
    h.robot.bus = Broken()
    await h.close(candle_at(29, "100", "100.5", "99.5", "100"))

    assert h.journal.codes() == ["approved"]
