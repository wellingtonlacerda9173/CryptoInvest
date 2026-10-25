from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from engine.application.risk import ApprovedEntry, RiskState, evaluate_entry
from engine.domain.risk import RiskConfig
from engine.infrastructure.live.client import BinanceRestClient
from engine.infrastructure.live.config import LiveConfig
from engine.infrastructure.live.credentials import ApiCredentials
from engine.infrastructure.live.gateway import LiveGateway
from tests.fakes.fake_binance import API_KEY, API_SECRET, FakeBinance
from tests.unit.risk.factories import EMPTY_CALENDAR, NOW, history, market, rules, signal

D = Decimal
SYMBOL = "BTC/USDT"


async def no_sleep(_: float) -> None:
    return None


@dataclass
class Rig:
    fake: FakeBinance
    gateway: LiveGateway
    client: BinanceRestClient
    alerts: list[tuple[str, str]]


def fake_credentials() -> ApiCredentials:
    return ApiCredentials(API_KEY, API_SECRET, "local_fake")


def live_config(fake: FakeBinance, **overrides: Any) -> LiveConfig:
    base: dict[str, Any] = {
        "environment": "local_fake",
        "allow_local_fake": True,
        "base_url": fake.base_url,
        "allowed_symbols": frozenset({SYMBOL}),
        "read_only": False,
        "capital_cap_quote": D(5000),
        "request_timeout_seconds": 0.4,
        "read_retries": 2,
        "order_lookup_retries": 3,
        "protection_retries": 3,
    }
    return LiveConfig(**(base | overrides))


def build_rig(fake: FakeBinance, **overrides: Any) -> Rig:
    config = live_config(fake, **overrides)
    client = BinanceRestClient(config, fake_credentials(), sleep=no_sleep)
    alerts: list[tuple[str, str]] = []
    gateway = LiveGateway(
        config,
        client,
        risk_config=RiskConfig(taker_fee_rate=D(0)),
        now=lambda: NOW,
        sleep=no_sleep,
        on_alert=lambda level, msg: alerts.append((level, msg)),
    )
    return Rig(fake, gateway, client, alerts)


def approved_entry(
    entry: str = "100",
    stop: str = "95",
    target: str = "111",
    cid: str = "e-1",
    equity: int = 10_000,
    symbol: str = SYMBOL,
) -> ApprovedEntry:
    result = evaluate_entry(
        signal(entry, stop, target, symbol=symbol),
        state=RiskState.initial(D(equity), NOW),
        config=RiskConfig(taker_fee_rate=D(0)),
        rules=rules(symbol=symbol),
        positions=[],
        pending=[],
        market=market(symbol=symbol),
        history=history(symbol=symbol),
        calendar=EMPTY_CALENDAR,
        now=NOW,
        client_order_id=cid,
    )
    assert isinstance(result, ApprovedEntry), result
    return result


async def started_fake() -> AsyncIterator[FakeBinance]:
    fake = FakeBinance()
    await fake.start()
    try:
        yield fake
    finally:
        await fake.stop()
