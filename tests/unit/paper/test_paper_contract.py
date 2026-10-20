from typing import Any

import pytest

from engine.domain.gateway import ExchangeGateway
from engine.domain.order import Order
from engine.infrastructure.paper import PaperGateway
from engine.infrastructure.paper import gateway as gateway_module
from tests.contract.exchange_gateway import ExchangeGatewayContract
from tests.unit.paper.helpers import make


class TestPaperGatewayContract(ExchangeGatewayContract):
    revalidate_module = gateway_module

    @pytest.fixture
    def gateway(self) -> Any:
        return make()[0]

    def sent_orders(self, gateway: ExchangeGateway) -> list[Order]:
        assert isinstance(gateway, PaperGateway)
        return gateway.submitted_orders
