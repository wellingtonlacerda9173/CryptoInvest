import time
from decimal import Decimal

import pytest
import structlog

from engine.infrastructure.live.client import BinanceRestClient
from engine.infrastructure.live.errors import (
    ClockDriftError,
    ForbiddenEndpointError,
    IpBannedError,
    OrderOutcomeUnknownError,
    OutcomeUnknownError,
)
from tests.fakes.fake_binance import API_KEY, API_SECRET, FakeBinance
from tests.unit.live.support import Rig, approved_entry, build_rig, fake_credentials, live_config, no_sleep

D = Decimal
OPOCO = "/api/v3/orderList/opoco"


def opoco_posts(fake: FakeBinance) -> int:
    return len([r for r in fake.log if r.method == "POST" and r.path == OPOCO])


async def test_response_lost_after_acceptance_never_duplicates_the_order(rig: Rig) -> None:
    rig.fake.fail(OPOCO, method="POST", after=True)

    await rig.gateway.place_order(approved_entry())

    assert opoco_posts(rig.fake) == 1
    assert len(rig.fake.lists) == 1


async def test_timeout_before_acceptance_resends_same_ids_only_after_lookup(rig: Rig) -> None:
    rig.fake.fail(OPOCO, method="POST", kind="timeout")

    await rig.gateway.place_order(approved_entry())

    posts = [r for r in rig.fake.log if r.method == "POST" and r.path == OPOCO]
    assert len(posts) == 2
    assert posts[0].params["listClientOrderId"] == posts[1].params["listClientOrderId"]
    assert posts[0].params["workingClientOrderId"] == "e-1"
    lookups = [i for i, r in enumerate(rig.fake.log) if r.path == "/api/v3/orderList" and r.method == "GET"]
    first_post = next(i for i, r in enumerate(rig.fake.log) if r.path == OPOCO)
    assert lookups and lookups[0] > first_post
    assert len(rig.fake.lists) == 1


async def test_5xx_on_order_is_treated_as_unknown_and_not_blindly_resent(rig: Rig) -> None:
    rig.fake.fail(OPOCO, method="POST", status=503, after=True)

    await rig.gateway.place_order(approved_entry())

    assert opoco_posts(rig.fake) == 1


async def test_unresolvable_outcome_does_not_resend(rig: Rig) -> None:
    rig.fake.fail(OPOCO, method="POST", kind="timeout")
    rig.fake.fail("/api/v3/orderList", method="GET", times=50, status=503)

    with pytest.raises(OrderOutcomeUnknownError):
        await rig.gateway.place_order(approved_entry())

    assert opoco_posts(rig.fake) == 1


async def test_client_never_resends_a_write_after_timeout(rig: Rig) -> None:
    rig.fake.fail("/api/v3/order", method="POST", kind="timeout")

    with pytest.raises(OutcomeUnknownError):
        await rig.client.request("POST", "/api/v3/order", {"symbol": "BTCUSDT"}, signed=True)

    assert len([r for r in rig.fake.log if r.path == "/api/v3/order"]) == 1


async def test_reads_are_retried_on_5xx(rig: Rig) -> None:
    rig.fake.fail("/api/v3/account", times=2, status=502)

    balances = await rig.gateway.get_balances()

    assert balances["USDT"].free == D(5000)


async def test_429_waits_retry_after_and_retries(fake: FakeBinance) -> None:
    sleeps: list[float] = []

    async def record(seconds: float) -> None:
        sleeps.append(seconds)

    client = BinanceRestClient(live_config(fake), fake_credentials(), sleep=record)
    fake.fail("/api/v3/account", status=429, headers={"Retry-After": "7"})

    data = await client.request("GET", "/api/v3/account", signed=True)

    assert data["permissions"] == ["SPOT"]
    assert 7.0 in sleeps
    await client.aclose()


async def test_429_on_order_post_is_retried_safely(rig: Rig) -> None:
    rig.fake.fail(OPOCO, method="POST", status=429, headers={"Retry-After": "1"})

    await rig.gateway.place_order(approved_entry())

    assert len(rig.fake.lists) == 1


async def test_418_ban_blocks_all_later_requests_locally(fake: FakeBinance) -> None:
    clock = [time.time()]
    client = BinanceRestClient(live_config(fake), fake_credentials(), clock=lambda: clock[0], sleep=no_sleep)
    fake.fail("/api/v3/account", status=418, headers={"Retry-After": "120"})
    with pytest.raises(Exception, match="418"):
        await client.request("GET", "/api/v3/account", signed=True)
    seen = len(fake.log)

    with pytest.raises(IpBannedError):
        await client.request("GET", "/api/v3/account", signed=True)
    assert len(fake.log) == seen

    clock[0] += 121
    assert (await client.request("GET", "/api/v3/time"))["serverTime"] > 0
    await client.aclose()


async def test_clock_drift_above_limit_refuses_signed_requests(fake: FakeBinance) -> None:
    fake.clock_offset_ms = 5000
    rig = build_rig(fake, max_clock_drift_ms=1000)

    with pytest.raises(ClockDriftError):
        await rig.gateway.get_balances()

    assert [r.path for r in fake.log] == ["/api/v3/time"]
    await rig.client.aclose()


async def test_small_drift_is_compensated_by_timestamp_offset(fake: FakeBinance) -> None:
    fake.clock_offset_ms = 800
    rig = build_rig(fake, max_clock_drift_ms=1000, recv_window_ms=1000)

    assert (await rig.gateway.get_balances())["USDT"].free == D(5000)
    await rig.client.aclose()


async def test_only_api_v3_spot_endpoints_are_reachable(rig: Rig) -> None:
    for path in (
        "/sapi/v1/capital/withdraw/apply",
        "/fapi/v1/order",
        "/api/v3/../sapi/v1/x",
        "/sapi/v1/margin/order",
    ):
        with pytest.raises(ForbiddenEndpointError):
            await rig.client.request("POST", path, {}, signed=True)

    assert rig.fake.log == []


async def test_secrets_never_reach_logs_or_repr(rig: Rig) -> None:
    rig.fake.fail("/api/v3/account", status=502)
    with structlog.testing.capture_logs() as captured:
        await rig.gateway.get_balances()
        await rig.gateway.place_order(approved_entry())
    blob = repr(captured) + repr(fake_credentials()) + str(fake_credentials())

    assert API_SECRET not in blob
    assert API_KEY not in blob
    assert "signature" not in blob.lower() or "signature=" not in blob.lower()


async def test_restarted_gateway_adopts_an_existing_order_instead_of_duplicating_it(rig: Rig) -> None:
    approved = approved_entry()
    await rig.gateway.place_order(approved)
    restarted = build_rig(rig.fake)

    report = await restarted.gateway.place_order(approved)

    assert report.client_order_id == "e-1"
    assert len(rig.fake.lists) == 1
    assert len([r for r in rig.fake.writes() if r.path == OPOCO]) == 2
    await restarted.client.aclose()
