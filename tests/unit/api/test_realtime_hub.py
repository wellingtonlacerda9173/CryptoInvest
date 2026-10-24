"""Hub de tempo real: canais, retomada por last_event_id, backpressure, limite por usuario e desconexao do runner."""

import asyncio
import contextlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from api.runner_link import FeedDisconnectedError
from api.services.realtime import DISCONNECTED_TEXT, FEED_DOWN_TEXT, RealtimeHub
from tests.redis_fakes import FakeLink, FakeSource

T0 = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


def hub(**kwargs: Any) -> tuple[RealtimeHub, Clock]:
    clock = Clock()
    return RealtimeHub(None, clock=clock, **kwargs), clock


def drain(sub: Any) -> list[dict[str, Any]]:
    items = []
    while not sub.queue.empty():
        items.append(json.loads(sub.queue.get_nowait()))
    return items


def test_runner_kinds_map_to_panel_channels_and_unknown_kinds_are_dropped() -> None:
    h, _ = hub()
    sub = h.register("u1", 5)
    assert sub is not None
    sub.channels |= {"status", "alerts", "decisions"}
    h.ingest("state", {"state": "RUNNING", "kind": "state"})
    h.ingest("risk", {"action": "daily_loss_pause"})
    h.ingest("decisions", {"code": "no_signal"})
    h.ingest("orders", {"event": "entry_placed"})
    h.ingest("inexistente", {})
    messages = [m for m in drain(sub) if m["type"] == "event"]
    assert [m["channel"] for m in messages] == ["status", "alerts", "decisions"]
    assert messages[0]["data"]["state"] == "RUNNING"
    assert all(m["id"].startswith(h.epoch + ":") for m in messages)


def test_event_ids_are_sequential_and_replay_resumes_after_the_last_seen() -> None:
    h, _ = hub()
    for index in range(5):
        h.ingest("decisions", {"n": index})
    resumed, frames = h.replay(f"{h.epoch}:2", {"decisions"})
    assert resumed and [json.loads(f)["data"]["n"] for f in frames] == [2, 3, 4]
    assert h.replay(f"{h.epoch}:5", {"decisions"}) == (True, [])
    resumed, frames = h.replay(f"{h.epoch}:2", {"orders"})
    assert resumed and frames == []


@pytest.mark.parametrize("last", [None, "", "outra:1", "abc", "{epoch}:999", "x:y:z"])
def test_replay_refuses_unknown_epoch_future_ids_or_garbage(last: str | None) -> None:
    h, _ = hub()
    h.ingest("decisions", {})
    wanted = None if last is None else last.replace("{epoch}", h.epoch)
    assert h.replay(wanted, {"decisions"}) == (False, [])


def test_replay_refuses_when_the_gap_is_larger_than_the_buffer() -> None:
    h, _ = hub(buffer_size=10)
    for index in range(30):
        h.ingest("decisions", {"n": index})
    assert h.replay(f"{h.epoch}:3", {"decisions"}) == (False, [])
    resumed, frames = h.replay(f"{h.epoch}:25", {"decisions"})
    assert resumed and len(frames) == 5


def test_connection_limit_per_user_is_enforced_and_released() -> None:
    h, _ = hub()
    subs = [h.register("u1", 2) for _ in range(2)]
    assert all(subs) and h.register("u1", 2) is None
    assert h.register("u2", 2) is not None
    h.unregister(subs[0])  # type: ignore[arg-type]
    assert h.register("u1", 2) is not None
    assert h.clients == 3


def test_slow_client_drops_oldest_gets_resync_and_is_flagged_after_the_limit() -> None:
    h, _ = hub(queue_size=3, max_dropped=4)
    slow = h.register("slow", 5)
    fast = h.register("fast", 5)
    assert slow is not None and fast is not None
    slow.channels = fast.channels = {"decisions"}
    for index in range(5):
        h.ingest("decisions", {"n": index})
        drain(fast)
    assert (
        slow.queue.qsize() == 3 and slow.dropped == 3 and slow.needs_resync and not slow.overflowed.is_set()
    )
    assert [m["data"]["n"] for m in drain(slow)] == [2, 3, 4]
    for index in range(5, 12):
        h.ingest("decisions", {"n": index})
        drain(fast)
    assert slow.overflowed.is_set()
    assert not fast.overflowed.is_set() and fast.dropped == 0


def test_a_slow_client_never_blocks_ingestion_for_the_others() -> None:
    h, _ = hub(queue_size=2)
    slow, fast = h.register("a", 5), h.register("b", 5)
    assert slow and fast
    slow.channels = fast.channels = {"trades"}
    for index in range(50):
        h.ingest("trades", {"n": index})
    assert h.last_event_id().endswith(":50")


def test_runner_disconnection_is_announced_once_and_recovery_too() -> None:
    h, clock = hub(stale_seconds=90)
    sub = h.register("u", 5)
    assert sub is not None
    assert h.runner_connected is False
    h.ingest("heartbeat", {"state": "RUNNING"})
    assert h.runner_connected
    assert [m["connected"] for m in drain(sub) if m["type"] == "runner"] == [True]
    clock.now += timedelta(seconds=60)
    h.check_stale()
    assert h.runner_connected
    clock.now += timedelta(seconds=40)
    h.check_stale()
    h.check_stale()
    notices = [m for m in drain(sub) if m["type"] == "runner"]
    assert [n["connected"] for n in notices] == [False] and notices[0]["message"] == DISCONNECTED_TEXT
    h.ingest("heartbeat", {})
    assert [m["connected"] for m in drain(sub) if m["type"] == "runner"] == [True]


def test_control_messages_reach_every_client_regardless_of_channels() -> None:
    h, _ = hub()
    sub = h.register("u", 5)
    assert sub is not None and sub.channels == set()
    h.ingest("heartbeat", {})
    assert [m["type"] for m in drain(sub)] == ["runner"]


def test_event_time_comes_from_the_payload_when_valid() -> None:
    h, _ = hub()
    sub = h.register("u", 5)
    assert sub is not None
    sub.channels = {"equity"}
    h.ingest("equity", {"at": "2026-10-20T11:59:00+00:00"})
    h.ingest("equity", {"at": "lixo"})
    first, second = [m for m in drain(sub) if m["type"] == "event"]
    assert first["at"].startswith("2026-10-20T11:59:00") and second["at"].startswith("2026-10-20T12:00:00")


async def test_run_consumes_the_source_reconnects_after_a_drop_and_flags_the_feed_down() -> None:
    clock = Clock()
    source = FakeSource()
    h = RealtimeHub(source, clock=clock)
    sub = h.register("u", 5)
    assert sub is not None
    sub.channels = {"decisions"}
    task = asyncio.create_task(h.run())
    source.push("decisions", {"n": 1})
    await asyncio.sleep(0.05)
    assert h.runner_connected
    source.drop()
    await asyncio.sleep(0.05)
    assert not h.runner_connected
    notices = [m for m in drain(sub) if m["type"] == "runner"]
    assert notices[-1]["message"] == FEED_DOWN_TEXT
    await asyncio.sleep(1.1)
    assert source.opened == 2
    source.push("decisions", {"n": 2})
    await asyncio.sleep(0.05)
    assert h.runner_connected
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_seed_from_the_last_known_heartbeat_marks_the_runner_connected() -> None:
    link = FakeLink()
    link.latest["heartbeat"] = {"state": "RUNNING", "at": T0.isoformat()}
    h = RealtimeHub(link.events(), clock=Clock(), link=link)
    task = asyncio.create_task(h.run())
    await asyncio.sleep(0.05)
    assert h.runner_connected and h.last_event_id().endswith(":1")
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def test_feed_disconnected_error_is_a_connection_error() -> None:
    assert issubclass(FeedDisconnectedError, ConnectionError)
