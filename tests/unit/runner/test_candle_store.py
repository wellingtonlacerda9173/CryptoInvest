"""Persistencia continua de velas, backfill de lacunas e guarda de feed obsoleto."""

import asyncio
from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from engine.domain.candle import Candle
from engine.domain.types import Symbol, Timeframe
from engine.runner import paper_robot
from engine.runner.candle_store import GapKey, backfill_store, persist_closed
from engine.runner.paper_robot import BotState
from tests.unit.backtest.engine_helpers import T0
from tests.unit.runner.fakes import FakeFeed, Harness, MemoryJournal, candle_at, config, warmup
from tests.unit.runner.test_paper_robot import FILL_CANDLE, QUIET, SIGNAL_CANDLE, STOP_CANDLE

H1 = Timeframe.H1


class MemoryStore:
    def __init__(self, candles: Sequence[Candle] = ()) -> None:
        self.rows: dict[tuple[str, datetime], Candle] = {(c.symbol, c.open_time): c for c in candles}
        self.upserts = 0
        self.fail = False

    async def upsert_many(self, candles: Sequence[Candle]) -> int:
        if self.fail:
            raise ConnectionError("banco fora")
        if any(not c.closed for c in candles):
            raise ValueError("only closed candles can be persisted")
        self.upserts += 1
        self.rows.update({(c.symbol, c.open_time): c for c in candles})
        return len(candles)

    async def fetch_range(
        self, symbol: Symbol, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[Candle]:
        return sorted(
            (c for (s, t), c in self.rows.items() if s == symbol and start <= t < end),
            key=lambda c: c.open_time,
        )


def eth(i: int) -> Candle:
    return candle_at(i, *QUIET).model_copy(update={"symbol": "ETH/USDT"})


def opens(store: MemoryStore, symbol: str) -> list[int]:
    return sorted(int((t - T0) / timedelta(hours=1)) for (s, t) in store.rows if s == symbol)


async def test_closed_candle_is_persisted_once_and_idempotently() -> None:
    store = MemoryStore()
    h = Harness(strategy_indexes=set())
    h.robot._candle_store = store
    await h.start()
    candle = candle_at(29, *QUIET)
    await h.close(candle)
    await h.robot.on_candle(candle)

    assert opens(store, "BTC/USDT") == list(range(30))
    assert store.upserts == 2


async def test_persistence_failure_never_stops_the_robot() -> None:
    store = MemoryStore()
    store.fail = True
    h = Harness(strategy_indexes=set())
    h.robot._candle_store = store
    await h.start()
    await h.close(candle_at(29, *QUIET))

    assert h.journal.codes() == ["no_signal"]
    assert not await persist_closed(store, [candle_at(30, *QUIET)])


async def test_eth_is_persisted_as_well_as_btc() -> None:
    store = MemoryStore()
    feed = FakeFeed([*warmup(), *(eth(i) for i in range(29))])
    h = Harness(strategy_indexes=set(), cfg=config(symbols=("BTC/USDT", "ETH/USDT")), feed=feed)
    h.robot._candle_store = store
    await h.start()
    await h.close(candle_at(29, *QUIET))
    await h.close(eth(29))

    assert 29 in opens(store, "BTC/USDT") and 29 in opens(store, "ETH/USDT")
    assert opens(store, "ETH/USDT") == list(range(30))


async def test_backfill_fills_the_gap_left_by_an_outage_and_is_idempotent() -> None:
    feed = FakeFeed([candle_at(i, *QUIET) for i in range(40)])
    store = MemoryStore([candle_at(i, *QUIET) for i in (*range(0, 10), *range(25, 30))])
    now = T0 + timedelta(hours=40, minutes=2)

    first = await backfill_store(store, feed, "BTC/USDT", H1, now, limit=40)

    assert first.unrecovered == 0 and first.gaps == 2
    assert opens(store, "BTC/USDT") == list(range(40))
    calls = len(feed.fetch_calls)
    again = await backfill_store(store, feed, "BTC/USDT", H1, now, limit=40)
    assert again.gaps == 0 and len(feed.fetch_calls) == calls


async def test_backfill_reports_unrecoverable_gap_without_raising() -> None:
    feed = FakeFeed([candle_at(i, *QUIET) for i in range(20)])
    store = MemoryStore([candle_at(i, *QUIET) for i in range(10)])
    now = T0 + timedelta(hours=25)

    result = await backfill_store(store, feed, "BTC/USDT", H1, now, limit=25)

    assert result.unrecovered == 4
    feed.fail_fetch = True
    failed = await backfill_store(MemoryStore(), feed, "BTC/USDT", H1, now, limit=25)
    assert failed.unrecovered == 25


async def test_robot_backfills_the_store_at_start_without_lookahead() -> None:
    store = MemoryStore()
    h = Harness(strategy_indexes=set())
    h.robot._candle_store = store
    await h.start()

    assert opens(store, "BTC/USDT") == list(range(29))
    assert all(c.close_time <= h.clock.now() for c in store.rows.values())


async def test_stale_candle_feed_refuses_entries_with_clear_code() -> None:
    h = Harness(cfg=config(symbols=("BTC/USDT", "ETH/USDT")))
    await h.start()
    await h.close(candle_at(29, *SIGNAL_CANDLE))

    assert h.journal.decisions[-1].code == "market_data_stale"
    assert h.journal.orders == {}


async def test_stale_feed_is_reported_and_clears_when_candles_return() -> None:
    h = Harness(strategy_indexes=set())
    await h.start()
    await h.close(candle_at(29, *QUIET))
    h.clock.advance(seconds=3600 * 2 + 300)
    await h.robot.housekeeping()

    status = h.robot.status_payload(h.clock.now())
    assert h.robot.bot_state is BotState.PAUSED
    assert list(status["market_data_stale"]) == ["BTC/USDT"]
    assert status["market_data_stale"]["BTC/USDT"] >= 7200
    assert status["pause_reasons"][-1]["automatic"] is True
    assert "desatualizados" in status["pause_reasons"][-1]["reason"]
    assert h.bus.of("alerts")[-1]["alert"] == "market_data_stale"

    await h.close(candle_at(30, *QUIET))
    await h.close(candle_at(31, *QUIET))
    await h.robot.housekeeping()
    assert (
        h.robot.bot_state is BotState.RUNNING
        and not h.robot.status_payload(h.clock.now())["market_data_stale"]
    )


async def test_stale_feed_still_manages_open_position_stop() -> None:
    h = await Harness().start()
    await h.close(candle_at(29, *SIGNAL_CANDLE))
    await h.close(candle_at(30, *FILL_CANDLE))
    h.clock.advance(seconds=3600 * 2)
    await h.robot.housekeeping()
    assert h.robot._feed_stale

    await h.robot.on_candle(candle_at(31, *STOP_CANDLE))

    [trade] = h.journal.trades
    assert trade.exit_reason.value == "stop_loss"
    assert h.robot.live_symbols == []


class HangingStore(MemoryStore):
    async def upsert_many(self, candles: Sequence[Candle]) -> int:
        await asyncio.sleep(60)
        return 0


def bad(i: int) -> Candle:
    return candle_at(i, *QUIET).model_copy(
        update={
            "open": Decimal("1.23"),
            "close": Decimal("1.23"),
            "low": Decimal("1.2"),
            "volume": Decimal(1),
        }
    )


async def test_backfill_reconciles_existing_synthetic_candles_with_rest() -> None:
    feed = FakeFeed([candle_at(i, *QUIET) for i in range(40)])
    store = MemoryStore([bad(i) if i in (3, 17) else candle_at(i, *QUIET) for i in range(40)])
    now = T0 + timedelta(hours=40, minutes=2)

    untouched = await backfill_store(store, feed, "BTC/USDT", H1, now, limit=40)
    assert untouched.reconciled == 0 and store.rows[("BTC/USDT", T0 + timedelta(hours=3))].close == Decimal(
        "1.23"
    )

    result = await backfill_store(store, feed, "BTC/USDT", H1, now, limit=40, reconcile=True)

    assert result.reconciled == 2
    assert all(store.rows[("BTC/USDT", c.open_time)] == c for c in feed.candles)
    again = await backfill_store(store, feed, "BTC/USDT", H1, now, limit=40, reconcile=True)
    assert again.reconciled == 0


async def test_reconcile_never_touches_the_open_boundary_candle() -> None:
    feed = FakeFeed([candle_at(i, *QUIET) for i in range(41)])
    store = MemoryStore([candle_at(i, *QUIET) for i in range(40)])
    store.rows[("BTC/USDT", T0 + timedelta(hours=40))] = bad(40)
    now = T0 + timedelta(hours=40, minutes=2)

    await backfill_store(store, feed, "BTC/USDT", H1, now, limit=40, reconcile=True)

    assert store.rows[("BTC/USDT", T0 + timedelta(hours=40))].close == Decimal("1.23")


async def test_permanent_gap_is_attempted_and_reported_only_once() -> None:
    feed = FakeFeed([candle_at(i, *QUIET) for i in (*range(10), *range(15, 41))])
    store = MemoryStore([candle_at(i, *QUIET) for i in (*range(10), *range(15, 41))])
    known: set[GapKey] = set()
    store.rows = {k: v for k, v in store.rows.items() if not 10 <= (k[1] - T0) // timedelta(hours=1) < 15}
    now = T0 + timedelta(hours=40, minutes=2)

    first = await backfill_store(store, feed, "BTC/USDT", H1, now, limit=40, known_gaps=known)
    calls = len(feed.fetch_calls)
    await backfill_store(store, feed, "BTC/USDT", H1, now, limit=40, known_gaps=known)
    second = await backfill_store(
        store, feed, "BTC/USDT", H1, now + timedelta(hours=1), limit=40, known_gaps=known
    )

    assert first.unrecovered == 5 and second.unrecovered == 0
    assert len(feed.fetch_calls) == calls


async def test_robot_alerts_a_permanent_store_gap_only_on_the_first_sync() -> None:
    store = MemoryStore()
    h = Harness(strategy_indexes=set())
    h.robot._candle_store = store
    await h.start()
    alerts = lambda: [a for a in h.bus.of("alerts") if a["alert"] == "candle_store_gap"]  # noqa: E731
    assert len(alerts()) == 1
    calls = len(h.feed.fetch_calls)

    await h.robot._sync_candle_store(h.clock.now())
    await h.robot._sync_candle_store(h.clock.now())

    assert len(alerts()) == 1 and len(h.feed.fetch_calls) == calls


async def test_robot_start_reconciles_a_synthetic_row_in_the_store() -> None:
    store = MemoryStore([bad(5)])
    h = Harness(strategy_indexes=set())
    h.robot._candle_store = store
    await h.start()

    assert store.rows[("BTC/USDT", T0 + timedelta(hours=5))] == h.feed.candles[5]


async def test_boundary_candle_not_yet_on_the_exchange_is_not_unrecovered() -> None:
    feed = FakeFeed([candle_at(i, *QUIET) for i in range(39)])
    store = MemoryStore([candle_at(i, *QUIET) for i in range(39)])

    result = await backfill_store(store, feed, "BTC/USDT", H1, T0 + timedelta(hours=40, minutes=2), limit=40)

    assert result.unrecovered == 0


async def test_slow_database_does_not_block_the_stop_and_alerts_after_three_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(paper_robot, "CANDLE_PERSIST_TIMEOUT_SECONDS", 0.05)
    h = await Harness().start()
    h.robot._candle_store = HangingStore()

    await asyncio.wait_for(h.close(candle_at(29, *SIGNAL_CANDLE)), 5)
    await asyncio.wait_for(h.close(candle_at(30, *FILL_CANDLE)), 5)
    assert [a for a in h.bus.of("alerts") if a["alert"] == "candle_persist_failing"] == []
    await asyncio.wait_for(h.close(candle_at(31, *STOP_CANDLE)), 5)

    [trade] = h.journal.trades
    assert trade.exit_reason.value == "stop_loss"
    assert len([a for a in h.bus.of("alerts") if a["alert"] == "candle_persist_failing"]) == 1
    h.robot._candle_store = MemoryStore()
    await h.close(candle_at(32, *QUIET))
    assert h.robot._persist_failures == 0


async def test_halt_and_manual_pause_are_not_overwritten_by_stale_feed() -> None:
    halted = MemoryJournal()
    halted.inherited_halt = ("run-antigo", "drawdown 0.0600")
    h = Harness(strategy_indexes=set(), journal=halted)
    await h.start()
    h.clock.advance(seconds=3600 * 3)
    await h.robot.housekeeping()
    status = h.robot.status_payload(h.clock.now())
    assert h.robot.bot_state is BotState.HALTED and status["halted"]
    assert status["market_data_stale"]

    p = await Harness(strategy_indexes=set()).start()
    assert await p.robot._pause("manutencao", operator="ana")
    p.clock.advance(seconds=3600 * 3)
    await p.robot.housekeeping()
    reasons = p.robot.status_payload(p.clock.now())["pause_reasons"]
    assert [r["operator"] for r in reasons] == ["ana", "system"]
    assert reasons[0]["reason"] == "manutencao" and reasons[1]["automatic"] is True
    assert p.robot.bot_state is BotState.PAUSED
    await p.close(candle_at(30, *QUIET))
    await p.close(candle_at(31, *QUIET))
    await p.robot.housekeeping()
    assert (
        p.robot.bot_state is BotState.PAUSED
        and not p.robot.status_payload(p.clock.now())["market_data_stale"]
    )
