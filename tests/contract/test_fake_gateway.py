from decimal import Decimal
from typing import Any

import pytest

from engine.domain.gateway import ExchangeGateway
from engine.domain.order import Order
from tests.contract import fakes
from tests.contract.exchange_gateway import ExchangeGatewayContract
from tests.contract.fakes import FakeExchangeGateway, UnsafeExchangeGateway


class TestFakeExchangeGateway(ExchangeGatewayContract):
    revalidate_module = fakes

    @pytest.fixture
    def gateway(self) -> Any:
        return FakeExchangeGateway()

    def sent_orders(self, gateway: ExchangeGateway) -> list[Order]:
        assert isinstance(gateway, FakeExchangeGateway)
        return gateway.sent


class _UnsafeContract(ExchangeGatewayContract):
    revalidate_module = fakes

    def sent_orders(self, gateway: ExchangeGateway) -> list[Order]:
        assert isinstance(gateway, UnsafeExchangeGateway)
        return gateway.sent


async def test_contract_rejects_gateway_that_skips_revalidation() -> None:
    contract = _UnsafeContract()

    with pytest.raises(pytest.fail.Exception):
        await contract.test_place_order_refuses_tampered_entry_order(
            UnsafeExchangeGateway(), {"stop_loss": Decimal(200)}
        )

    with pytest.MonkeyPatch.context() as patch, pytest.raises(AssertionError):
        await contract.test_place_order_calls_revalidate_order(UnsafeExchangeGateway(), patch)
