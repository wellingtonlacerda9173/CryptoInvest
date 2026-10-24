"""QA final: a retomada (send_burst) nunca e ultrapassada por eventos ao vivo da mesma conexao."""

import asyncio
import contextlib
from typing import Any

from api.routers.ws import Connection


class SlowSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, text: str) -> None:
        await asyncio.sleep(0.01)
        self.sent.append(text)


async def test_live_event_sent_during_a_burst_arrives_after_the_whole_burst() -> None:
    socket = SlowSocket()
    conn = Connection(socket, sub=None, timeout=5)  # type: ignore[arg-type]
    burst = [f"replay-{i}" for i in range(8)]

    async def live() -> None:
        await asyncio.sleep(0.025)  # chega no meio da retomada
        await conn.send("live-1")

    await asyncio.gather(conn.send_burst(burst), live())

    assert socket.sent == [*burst, "live-1"]


async def test_two_bursts_never_interleave() -> None:
    socket = SlowSocket()
    conn = Connection(socket, sub=None, timeout=5)  # type: ignore[arg-type]
    first, second = ["a1", "a2", "a3"], ["b1", "b2", "b3"]
    await asyncio.gather(conn.send_burst(first), conn.send_burst(second))
    assert socket.sent in ([*first, *second], [*second, *first])


async def test_a_burst_that_times_out_releases_the_lock_for_the_next_sender() -> None:
    class Stuck(SlowSocket):
        calls = 0

        async def send_text(self, text: str) -> None:
            Stuck.calls += 1
            if Stuck.calls == 1:
                await asyncio.sleep(10)
            await super().send_text(text)

    conn = Connection(Stuck(), sub=None, timeout=0.05)  # type: ignore[arg-type]
    with contextlib.suppress(TimeoutError):
        await conn.send_burst(["x"])
    await asyncio.wait_for(conn.send("y"), 2)
    assert conn.ws.sent == ["y"]  # type: ignore[attr-defined]


_ = Any
