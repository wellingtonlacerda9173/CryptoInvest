"""Snapshot do runner: vivo = evento mais novo de qualquer tipo; conteudo (estado/risco/versao) so de heartbeat/state."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from api.services.runner_status import RunnerStatusService
from tests.redis_fakes import FakeLink

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def container(link: FakeLink) -> Any:
    return SimpleNamespace(
        link=link,
        clock=lambda: NOW,
        settings=SimpleNamespace(runner_heartbeat_stale_seconds=15, runner_data_dir=None),
    )


def event(kind: str, age: float, **extra: Any) -> dict[str, Any]:
    stamp = (NOW - timedelta(seconds=age)).isoformat()
    base: dict[str, Any] = {"kind": kind, "run_id": "r1", "at": stamp}
    if kind != "equity":
        base |= {
            "heartbeat_at": stamp,
            "state": "RUNNING",
            "risk": {"risk_per_trade": "0.01"},
            "config_version": 3,
        }
    return base | extra


async def snapshot(latest: dict[str, dict[str, Any]]) -> Any:
    link = FakeLink()
    link.latest = latest
    return await RunnerStatusService(container(link)).snapshot()


async def test_freshest_equity_keeps_the_risk_of_the_last_heartbeat() -> None:
    snap = await snapshot({"heartbeat": event("heartbeat", 3), "equity": event("equity", 0.5, equity="1")})

    assert snap.data is not None and snap.data["risk"] == {"risk_per_trade": "0.01"}
    assert snap.is_on and snap.run_id == "r1" and snap.age_seconds == pytest.approx(0.5)


async def test_equity_alone_keeps_the_robot_alive_and_stale_content_is_not_invented() -> None:
    snap = await snapshot({"equity": event("equity", 1, equity="1")})

    assert snap.alive and snap.data is not None and "risk" not in snap.data


async def test_state_newer_than_heartbeat_wins_the_content() -> None:
    state = event("state", 1, state="PAUSED", paused=True, config_version=4)
    snap = await snapshot({"heartbeat": event("heartbeat", 6), "state": state})

    assert snap.state == "PAUSED" and snap.data is not None and snap.data["config_version"] == 4


KINDS = st.sampled_from(["heartbeat", "state", "equity"])
# Idades em milissegundos inteiros: o carimbo ISO tem resolucao de microssegundo, entao floats arbitrarios
# (subnormais, diferencas < 1us) colapsam no mesmo instante e tornam o "mais novo" ambiguo.
AGES = st.integers(0, 14_000).map(lambda ms: ms / 1000)


@settings(max_examples=150, deadline=None)
@given(
    ages=st.dictionaries(KINDS, AGES, min_size=1),
    versions=st.fixed_dictionaries({"heartbeat": st.integers(1, 9), "state": st.integers(1, 9)}),
)
async def test_content_always_comes_from_the_newest_heartbeat_or_state(
    ages: dict[str, float], versions: dict[str, int]
) -> None:
    assume(ages.get("heartbeat") != ages.get("state") or "heartbeat" not in ages)
    latest = {
        kind: event(kind, age, **({} if kind == "equity" else {"config_version": versions[kind]}))
        for kind, age in ages.items()
    }
    snap = await snapshot(latest)

    content = {k: v for k, v in ages.items() if k != "equity"}
    assert snap.alive and snap.age_seconds == pytest.approx(min(ages.values()), abs=1e-3)
    assert snap.data is not None
    if content:
        newest = min(content, key=lambda k: content[k])
        assert snap.data["config_version"] == versions[newest]
        assert snap.data["risk"] == {"risk_per_trade": "0.01"}
    else:
        assert "risk" not in snap.data
