"""Escrita do /ws: cliente lento recebe `resync`, e e desconectado se nao der conta (sem travar os outros)."""

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

from starlette.websockets import WebSocketState

from api.routers.ws import Connection, _writer
from api.services.realtime import RealtimeHub, Subscription
from api.ws_schema import CLOSE_TRY_AGAIN


class StubSocket:
    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay
        self.sent: list[dict[str, Any]] = []
        self.closed: tuple[int, str] | None = None
        self.application_state = WebSocketState.CONNECTED

    async def send_text(self, text: str) -> None:
        await asyncio.sleep(self.delay)
        self.sent.append(json.loads(text))

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = (code, reason)
        self.application_state = WebSocketState.DISCONNECTED


def subscription(size: int = 3, max_dropped: int = 2) -> Subscription:
    return Subscription("u", asyncio.Queue(size), max_dropped)


def frame(n: int) -> str:
    return json.dumps({"type": "event", "n": n})


async def test_resync_is_sent_before_the_next_event_after_messages_were_dropped() -> None:
    sub = subscription(size=2, max_dropped=10)
    sub.channels = {"decisions"}
    for index in range(5):
        sub.offer("decisions", frame(index))
    assert sub.needs_resync and sub.dropped == 3
    socket = StubSocket()
    task = asyncio.create_task(_writer(Connection(socket, sub, 1.0)))  # type: ignore[arg-type]
    await asyncio.sleep(0.05)
    task.cancel()
    assert [m["type"] for m in socket.sent] == ["resync", "event", "event"]
    assert [m.get("n") for m in socket.sent[1:]] == [3, 4]
    assert "releia" in socket.sent[0]["reason"]


async def test_overflowing_the_limit_closes_with_try_again_and_stops_the_writer() -> None:
    sub = subscription(size=2, max_dropped=2)
    sub.channels = {"decisions"}
    for index in range(10):
        sub.offer("decisions", frame(index))
    assert sub.overflowed.is_set()
    socket = StubSocket()
    await asyncio.wait_for(_writer(Connection(socket, sub, 1.0)), 1)  # type: ignore[arg-type]
    assert socket.closed is not None and socket.closed[0] == CLOSE_TRY_AGAIN and socket.sent == []


async def test_a_socket_that_blocks_on_send_is_closed_by_the_timeout() -> None:
    sub = subscription()
    sub.channels = {"decisions"}
    sub.offer("decisions", frame(1))
    socket = StubSocket(delay=5)
    await asyncio.wait_for(_writer(Connection(socket, sub, 0.05)), 1)  # type: ignore[arg-type]
    assert socket.closed is not None and socket.closed[0] == CLOSE_TRY_AGAIN


async def test_one_slow_client_does_not_delay_the_fast_one() -> None:
    hub = RealtimeHub(None, clock=lambda: datetime.now(UTC), queue_size=5)
    slow, fast = hub.register("slow", 5), hub.register("fast", 5)
    assert slow and fast
    slow.channels = fast.channels = {"decisions"}
    slow_socket, fast_socket = StubSocket(delay=0.5), StubSocket()
    tasks = [
        asyncio.create_task(_writer(Connection(slow_socket, slow, 5))),  # type: ignore[arg-type]
        asyncio.create_task(_writer(Connection(fast_socket, fast, 5))),  # type: ignore[arg-type]
    ]
    for index in range(30):
        hub.ingest("decisions", {"n": index})
        await asyncio.sleep(0)
    await asyncio.sleep(0.2)
    for task in tasks:
        task.cancel()
    assert len([m for m in fast_socket.sent if m["type"] == "event"]) == 30
    assert len(slow_socket.sent) < 5 and slow.dropped > 0


def event_frame(seq: int, channel: str = "decisions") -> str:
    return json.dumps({"type": "event", "id": f"a1b2c3d4:{seq}", "channel": channel, "data": {}})


async def test_replayed_and_live_copies_of_the_same_event_are_sent_once() -> None:
    sub = subscription(size=10)
    sub.channels = {"decisions"}
    socket = StubSocket()
    conn = Connection(socket, sub, 1.0)  # type: ignore[arg-type]
    assert conn.first_delivery(event_frame(5)) is True
    for seq in (5, 6, 6):
        sub.offer("decisions", event_frame(seq))
    task = asyncio.create_task(_writer(conn))
    await asyncio.sleep(0.05)
    task.cancel()
    assert [m["id"] for m in socket.sent] == ["a1b2c3d4:6"]


async def test_control_frames_are_never_deduplicated() -> None:
    conn = Connection(StubSocket(), subscription(), 1.0)  # type: ignore[arg-type]
    notice = json.dumps({"type": "runner", "connected": True})
    assert conn.first_delivery(notice) and conn.first_delivery(notice)


def test_hub_caps_total_connections_across_users() -> None:
    hub = RealtimeHub(None, clock=lambda: datetime.now(UTC), max_total=3)
    kept = [hub.register(f"u{i}", 5) for i in range(3)]
    assert all(kept) and hub.register("another", 5) is None
    hub.unregister(kept[0])  # type: ignore[arg-type]
    assert hub.register("another", 5) is not None


async def test_live_events_never_overtake_the_replay_burst_of_a_resubscription() -> None:
    from api.routers.ws import _handle
    from api.ws_schema import SubscribeMessage

    hub = RealtimeHub(None, clock=lambda: datetime.now(UTC))
    for n in range(6):
        hub.ingest("decisions", {"n": n})
    sub = hub.register("u", 5)
    assert sub is not None
    socket = StubSocket(delay=0.01)
    conn = Connection(socket, sub, 1.0)  # type: ignore[arg-type]
    writer = asyncio.create_task(_writer(conn))
    last = f"{hub.epoch}:1"
    handler = asyncio.create_task(
        _handle(conn, hub, SubscribeMessage(type="subscribe", channels=["decisions"], last_event_id=last))
    )
    for n in range(6, 10):
        await asyncio.sleep(0.015)
        hub.ingest("decisions", {"n": n})
    await handler
    await asyncio.sleep(0.2)
    writer.cancel()

    ids = [int(m["id"].split(":")[1]) for m in socket.sent if m["type"] == "event"]
    assert ids == sorted(ids) and len(ids) == len(set(ids)), ids
    assert ids == list(range(2, 11))
