"""QA rodada FINAL: A2 de ponta a ponta (API + Redis/Postgres reais + PaperRobot) com heartbeat/state/equity
intercalados em fases e cadencias variadas. Reducao com o robo ligado SEMPRE vira comando; aumento nunca e imediato."""

import json
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest

from tests.integration.api.runner_support import module_db_url, module_sessions, redis_link
from tests.integration.api.test_runner_chain import Chain, applied, chain

pytestmark = pytest.mark.integration
__all__ = ["chain", "module_db_url", "module_sessions", "redis_link"]

ORDERS = {
    "hb,state,eq": ("heartbeat", "state", "equity"),
    "state,hb,eq": ("state", "heartbeat", "equity"),
    "hb,eq,eq": ("heartbeat", "equity", "equity"),
    "eq,hb,eq": ("equity", "heartbeat", "equity"),
    "state,eq": ("state", "equity"),
    "hb,eq": ("heartbeat", "equity"),
}


async def emit(chain: Chain, kind: str) -> None:
    robot = chain.h.robot
    chain.h.clock.set(chain.api.clock())
    now = chain.h.clock.now()
    if kind == "heartbeat":
        await robot._emit("heartbeat", robot.status_payload(now))
    elif kind == "state":
        await robot._publish_state("tick")
    else:
        await robot._equity(now)
    import asyncio

    await asyncio.sleep(0.02)


async def sync_robot_with_saved(chain: Chain) -> None:
    """Banco compartilhado no modulo: o robo novo parte do risco que o ultimo teste salvou."""
    saved = (await chain.admin.get("/settings")).json()["document"]["risk"]
    risk = chain.h.robot.risk
    chain.h.robot.risk = risk.model_copy(
        update={
            k: Decimal(str(v))
            for k, v in saved.items()
            if k in type(risk).model_fields and isinstance(getattr(risk, k), Decimal)
        }
    )
    await emit(chain, "state")


async def put_risk(chain: Chain, value: dict[str, Any], **extra: Any) -> Any:
    version = (await chain.admin.get("/settings")).json()["version"]
    return await chain.admin.send(
        "PUT",
        "/settings/risk",
        json={
            "value": value,
            "reason": "qa final",
            "expected_version": 0 if version is None else version["id"],
            **extra,
        },
    )


@pytest.mark.parametrize("step", [0.5, 0.9, 1.0, 2.0])
@pytest.mark.parametrize("order", list(ORDERS))
async def test_reduction_with_the_robot_on_is_always_a_command_whatever_the_event_interleaving(
    chain: Chain, order: str, step: float
) -> None:
    await chain.announce()
    await sync_robot_with_saved(chain)
    current = Decimal(str(chain.h.robot.risk.risk_per_trade))
    lower = (current * Decimal("0.9")).quantize(Decimal("0.00001"))
    for kind in ORDERS[order]:
        chain.api.clock.advance(seconds=step)
        await emit(chain, kind)

    response = await put_risk(chain, {"risk_per_trade": str(lower)})

    body = response.json()
    assert response.status_code == 200, response.text
    assert body["activation"] == "immediate", body
    assert body["command"] is not None and body["command"]["state"] == "delivered", body
    final = await applied(chain, body["command"]["id"])
    assert final["state"] == "applied"
    assert chain.h.robot.risk.risk_per_trade == lower
    history = (await chain.admin.get("/settings/history", params={"limit": 5})).json()["items"]
    assert next(v for v in history if v["id"] == body["version"]["id"])["application"] == "applied"


async def test_an_increase_with_the_robot_on_is_never_immediate_and_never_in_the_pushed_command(
    chain: Chain,
) -> None:
    await chain.announce()
    await sync_robot_with_saved(chain)
    for kind in ("heartbeat", "equity"):
        chain.api.clock.advance(seconds=0.9)
        await emit(chain, kind)
    current = Decimal(str(chain.h.robot.risk.risk_per_trade))
    higher = (current * Decimal("1.2")).quantize(Decimal("0.00001"))
    link = chain.api.container.link
    before = await link._client.llen(link.keys.commands)  # type: ignore[union-attr]

    response = await put_risk(chain, {"risk_per_trade": str(higher)}, acknowledge_risk_increase=True)

    body = response.json()
    assert response.status_code == 200, response.text
    assert body["activation"] == "next_start" and body["command"] is None, body
    assert await link._client.llen(link.keys.commands) == before  # type: ignore[union-attr]
    await chain.tick()
    assert chain.h.robot.risk.risk_per_trade == current


async def test_mixed_reduction_and_increase_pushes_only_the_reduction_and_reports_honestly(
    chain: Chain,
) -> None:
    await chain.announce()
    await sync_robot_with_saved(chain)
    chain.api.clock.advance(seconds=1)
    await emit(chain, "equity")
    robot = chain.h.robot
    lower = (Decimal(str(robot.risk.max_drawdown)) * Decimal("0.9")).quantize(Decimal("0.0001"))
    higher = (Decimal(str(robot.risk.risk_per_trade)) * Decimal("1.1")).quantize(Decimal("0.00001"))

    response = await put_risk(
        chain, {"max_drawdown": str(lower), "risk_per_trade": str(higher)}, acknowledge_risk_increase=True
    )

    body = response.json()
    assert response.status_code == 200, response.text
    assert body["command"] is not None
    final = await applied(chain, body["command"]["id"])
    assert final["state"] in {"applied", "partially_applied"}
    assert robot.risk.max_drawdown == lower and robot.risk.risk_per_trade != higher
