from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import pytest

from engine.domain.gateway import ExchangeGateway
from engine.domain.order import Order
from engine.infrastructure.live import gateway as gateway_module
from tests.contract.exchange_gateway import ExchangeGatewayContract
from tests.unit.live.support import approved_entry, build_rig, started_fake


class TestLiveGatewayContract(ExchangeGatewayContract):
    """O LiveGateway cumpre o contrato do ExchangeGateway contra a Binance FALSA."""

    revalidate_module = gateway_module

    @pytest.fixture
    async def gateway(self) -> AsyncIterator[Any]:
        async for fake in started_fake():
            fake.balances["BTC"][0] = Decimal(10)  # saldo ALHEIO: o gateway so vende o que a entrada deve
            fake.set_price("BTCUSDT", Decimal(99))  # a entrada executa e fica protegida: ha posicao a sair
            rig = build_rig(fake)
            try:
                yield rig.gateway
            finally:
                await rig.client.aclose()

    def sent_orders(self, gateway: ExchangeGateway) -> list[Order]:
        return gateway.submitted_orders()  # type: ignore[attr-defined, no-any-return]

    def entry_submission(self, **changes: Any) -> tuple[Any, Order]:
        approved = approved_entry(cid="entry-1")
        for field, value in changes.items():
            object.__setattr__(approved.order, field, value)
        return approved, approved.order


def test_live_gateway_satisfies_the_async_protected_protocol() -> None:
    from engine.domain.gateway import AsyncProtectedOrderGateway
    from engine.infrastructure.live.gateway import LiveGateway

    required = {"place_order", "cancel_order", "get_order", "get_balances", "raise_stop", "drop_target"}
    assert all(callable(getattr(LiveGateway, name, None)) for name in required)
    assert AsyncProtectedOrderGateway is not None
