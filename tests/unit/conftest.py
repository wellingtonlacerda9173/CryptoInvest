import pytest

from tests.redis_fakes import FakeRedis


@pytest.fixture
def fake_redis() -> FakeRedis:
    return FakeRedis()
