"""Allowlists do LiveConfig (host/ambiente) e do cliente ((metodo, caminho) exatos). Fake local apenas."""

from decimal import Decimal

import pytest

from engine.infrastructure.live.client import BinanceRestClient
from engine.infrastructure.live.config import LiveConfig
from engine.infrastructure.live.credentials import ApiCredentials
from engine.infrastructure.live.errors import ForbiddenEndpointError
from tests.fakes.fake_binance import API_KEY, API_SECRET, FakeBinance
from tests.unit.live.support import Rig, build_rig, live_config, no_sleep

D = Decimal


def cfg(**kw: object) -> LiveConfig:
    return LiveConfig(capital_cap_quote=D(100), **kw)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "host", ["api.binance.com", "api-gcp.binance.com", "api1.binance.com", "api4.binance.com"]
)
def test_mainnet_accepts_only_documented_hosts(host: str) -> None:
    assert cfg(environment="mainnet", base_url=f"https://{host}").base_url == f"https://{host}"


@pytest.mark.parametrize(
    "url",
    [
        "https://api5.binance.com",
        "https://api.binance.com.evil.example",
        "https://evil.example/api.binance.com",
        "https://api.binance.com@evil.example",
        "https://user:pw@api.binance.com",
        "https://api.binance.com:8443",
        "https://api.binance.com/api/v3",
        "https://api.binance.com?x=1",
        "http://api.binance.com",
        "https://testnet.binance.vision",
        "https://data-api.binance.vision",
        "https://api.binance.com.",
        "http://127.0.0.1:9",
        "",
    ],
)
def test_mainnet_rejects_everything_else(url: str) -> None:
    with pytest.raises(ValueError, match="base_url"):
        cfg(environment="mainnet", base_url=url)


def test_testnet_needs_explicit_flag_and_exact_host() -> None:
    with pytest.raises(ValueError, match="testnet=True"):
        cfg(environment="testnet", base_url="https://testnet.binance.vision")
    assert cfg(environment="testnet", testnet=True, base_url="https://testnet.binance.vision")
    with pytest.raises(ValueError, match="base_url"):
        cfg(environment="testnet", testnet=True, base_url="https://api.binance.com")
    with pytest.raises(ValueError, match="base_url"):
        cfg(environment="mainnet", base_url="https://testnet.binance.vision")
    with pytest.raises(ValueError, match="testnet=True"):
        cfg(environment="mainnet", testnet=True, base_url="https://api.binance.com")


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1:8080", "http://localhost:1", "http://[::1]:9", "http://127.0.0.1"]
)
def test_local_fake_exact_loopback_only_with_flag(url: str) -> None:
    assert cfg(environment="local_fake", allow_local_fake=True, base_url=url)
    with pytest.raises(ValueError, match="allow_local_fake"):
        cfg(environment="local_fake", base_url=url)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1.evil.example",
        "http://localhost.evil.example",
        "http://127.0.0.1@evil.example",
        "http://127.0.0.2:80",
        "https://127.0.0.1:8080",
        "http://127.0.0.1:80/x",
        "http://0.0.0.0:80",
    ],
)
def test_local_fake_rejects_lookalikes(url: str) -> None:
    with pytest.raises(ValueError, match="base_url"):
        cfg(environment="local_fake", allow_local_fake=True, base_url=url)


def test_allow_local_fake_is_invalid_in_real_environments() -> None:
    with pytest.raises(ValueError, match="allow_local_fake"):
        cfg(environment="mainnet", allow_local_fake=True, base_url="https://api.binance.com")


def test_environment_is_mandatory() -> None:
    with pytest.raises(ValueError, match="environment"):
        LiveConfig(base_url="https://api.binance.com", capital_cap_quote=D(1))  # type: ignore[call-arg]


def test_credentials_of_another_environment_are_refused(fake: FakeBinance) -> None:
    for env in ("mainnet", "testnet"):
        with pytest.raises(ForbiddenEndpointError, match="outro ambiente"):
            BinanceRestClient(live_config(fake), ApiCredentials(API_KEY, API_SECRET, env), sleep=no_sleep)
    with pytest.raises(ValueError, match="ambiente"):
        ApiCredentials(API_KEY, API_SECRET, "prod")


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("DELETE", "/api/v3/openOrders"),
        ("POST", "/api/v3/order/cancelReplace"),
        ("POST", "/api/v3/orderList/otoco"),
        ("POST", "/api/v3/userDataStream"),
        ("GET", "/api/v3/userDataStream"),
        ("DELETE", "/api/v3/userDataStream"),
        ("GET", "/api/v3/depth"),
        ("POST", "/api/v3/order/test"),
        ("PUT", "/api/v3/order"),
        ("GET", "/api/v3/order/"),
        ("GET", "/api/v3/ORDER"),
        ("GET", "/api/v3/account?x=1"),
        ("POST", "/sapi/v1/capital/withdraw/apply"),
        ("POST", "/sapi/v1/asset/transfer"),
        ("POST", "/sapi/v1/margin/order"),
        ("POST", "/fapi/v1/order"),
        ("POST", "/dapi/v1/order"),
        ("GET", "/api/v3"),
        ("GET", "//api/v3/account"),
        ("GET", "/api/v3/./account"),
        ("GET", "/api/v3/%2e%2e/sapi/v1/x"),
    ],
)
async def test_every_endpoint_outside_the_exact_allowlist_is_refused_before_the_network(
    rig: Rig, method: str, path: str
) -> None:
    with pytest.raises(ForbiddenEndpointError):
        await rig.client.request(method, path, {}, signed=True)
    assert rig.fake.log == []


async def test_redirects_are_never_followed(fake: FakeBinance) -> None:
    target = FakeBinance()
    await target.start()
    try:
        fake.redirect_to = target.base_url
        rig = build_rig(fake)
        with pytest.raises(ForbiddenEndpointError, match="redirecionamento"):
            await rig.client.request("GET", "/api/v3/account", signed=True)
        assert target.log == []
        await rig.client.aclose()
    finally:
        await target.stop()
