from collections.abc import AsyncIterator

import pytest

from tests.fakes.fake_binance import FakeBinance
from tests.unit.live.support import Rig, build_rig, started_fake


@pytest.fixture
async def fake() -> AsyncIterator[FakeBinance]:
    async for item in started_fake():
        yield item


@pytest.fixture
async def rig(fake: FakeBinance) -> AsyncIterator[Rig]:
    built = build_rig(fake)
    try:
        yield built
    finally:
        await built.client.aclose()
