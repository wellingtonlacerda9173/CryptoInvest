from datetime import timedelta
from decimal import Decimal

import pytest

from engine.application.risk.approval import ApprovalError, ApprovalExpiredError, ApprovedEntry
from engine.domain.gateway import DuplicateOrderError
from engine.infrastructure.live import gateway as gateway_module
from engine.infrastructure.live.errors import (
    EntryNotApprovedError,
    FilterViolationError,
    InsufficientBalanceError,
    ReadOnlyModeError,
    SymbolNotAllowedError,
)
from tests.fakes.fake_binance import FakeBinance
from tests.unit.live.support import NOW, Rig, approved_entry, build_rig

D = Decimal


async def test_plain_entry_order_without_seal_is_refused_without_traffic(rig: Rig) -> None:
    approved = approved_entry()

    with pytest.raises(EntryNotApprovedError):
        await rig.gateway.place_order(approved.order)

    assert rig.fake.log == []


async def test_forged_approval_is_refused(rig: Rig) -> None:
    approved = approved_entry()
    forged = object.__new__(ApprovedEntry)
    for name in ("order", "signal", "risk_amount", "guards"):
        object.__setattr__(forged, name, getattr(approved, name))
    object.__setattr__(forged, "_seal", b"x" * 32)

    with pytest.raises(ApprovalError):
        await rig.gateway.place_order(forged)

    assert rig.fake.writes() == []


async def test_tampered_approval_is_refused(rig: Rig) -> None:
    approved = approved_entry()
    object.__setattr__(approved.order, "quantity", D(500))

    with pytest.raises(ApprovalError):
        await rig.gateway.place_order(approved)

    assert rig.fake.writes() == []


async def test_expired_approval_is_refused(fake: FakeBinance) -> None:
    rig = build_rig(fake)
    rig.gateway._now = lambda: NOW + timedelta(days=1)

    with pytest.raises(ApprovalExpiredError):
        await rig.gateway.place_order(approved_entry())

    assert fake.writes() == []
    await rig.client.aclose()


async def test_place_order_always_calls_revalidate_order(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[object] = []
    original = gateway_module.revalidate_order
    monkeypatch.setattr(gateway_module, "revalidate_order", lambda o: (seen.append(o), original(o))[1])
    approved = approved_entry()

    await rig.gateway.place_order(approved)

    assert seen == [approved.order]


async def test_duplicate_client_order_id_is_refused_locally(rig: Rig) -> None:
    await rig.gateway.place_order(approved_entry())

    with pytest.raises(DuplicateOrderError):
        await rig.gateway.place_order(approved_entry())

    assert len(rig.fake.order_writes()) == 1


async def test_symbol_outside_allowed_list_is_refused(fake: FakeBinance) -> None:
    rig = build_rig(fake)

    with pytest.raises(SymbolNotAllowedError):
        await rig.gateway.place_order(approved_entry(symbol="ETH/USDT"))

    assert fake.writes() == []
    await rig.client.aclose()


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("step", D("0.3")),
        ("tick", D("0.5")),
        ("min_notional", D(5000)),
        ("oco_allowed", False),
        ("opo_allowed", False),
    ],
)
async def test_order_outside_symbol_filters_is_refused(
    fake: FakeBinance, setting: str, value: object
) -> None:
    setattr(fake, setting, value)
    rig = build_rig(fake)

    with pytest.raises(FilterViolationError):
        await rig.gateway.place_order(approved_entry(target="111.25"))

    assert fake.writes() == []
    await rig.client.aclose()


async def test_percent_price_filter_is_enforced(fake: FakeBinance) -> None:
    fake.prices["BTCUSDT"] = D(1000)
    rig = build_rig(fake)

    with pytest.raises(FilterViolationError, match="PERCENT_PRICE_BY_SIDE"):
        await rig.gateway.place_order(approved_entry())

    assert fake.writes() == []
    await rig.client.aclose()


async def test_capital_cap_limits_entries_and_reported_balance(fake: FakeBinance) -> None:
    rig = build_rig(fake, capital_cap_quote=D(800))

    balances = await rig.gateway.get_balances()
    assert balances["USDT"].free == D(800)
    with pytest.raises(InsufficientBalanceError):
        await rig.gateway.place_order(approved_entry())
    assert fake.writes() == []
    await rig.client.aclose()


async def test_real_insufficient_balance_is_refused(fake: FakeBinance) -> None:
    fake.balances["USDT"][0] = D(500)
    rig = build_rig(fake)

    with pytest.raises(InsufficientBalanceError):
        await rig.gateway.place_order(approved_entry())

    assert fake.writes() == []
    await rig.client.aclose()


async def test_committed_capital_reduces_next_entry_allowance(fake: FakeBinance) -> None:
    rig = build_rig(fake, capital_cap_quote=D(1500))
    await rig.gateway.place_order(approved_entry())

    assert (await rig.gateway.get_balances())["USDT"].free == D(500)
    with pytest.raises(InsufficientBalanceError):
        await rig.gateway.place_order(approved_entry(cid="e-2"))
    await rig.client.aclose()


async def test_read_only_mode_sends_no_order_post(fake: FakeBinance) -> None:
    rig = build_rig(fake, read_only=True)

    with pytest.raises(ReadOnlyModeError):
        await rig.gateway.place_order(approved_entry())
    with pytest.raises(ReadOnlyModeError):
        await rig.client.request("POST", "/api/v3/order", {"symbol": "BTCUSDT"}, signed=True)
    with pytest.raises(ReadOnlyModeError):
        await rig.client.request("DELETE", "/api/v3/order", {"symbol": "BTCUSDT"}, signed=True)

    assert fake.writes() == []
    assert (await rig.gateway.get_balances())["USDT"].free == D(5000)
    await rig.client.aclose()
