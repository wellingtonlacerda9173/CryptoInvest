from collections.abc import Iterator

import pytest

from tests.unit.regime_stub import stub_uptrend


@pytest.fixture(autouse=True)
def _regime_uptrend(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> Iterator[None]:
    yield from stub_uptrend(monkeypatch, request)
