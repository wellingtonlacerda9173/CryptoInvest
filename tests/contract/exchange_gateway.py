from datetime import UTC, datetime
from decimal import Decimal
from types import ModuleType
from typing import Any

import pytest
from pydantic import ValidationError

from engine.domain.gateway import ExchangeGateway
from engine.domain.order import EntryOrder, ExitOrder, Order, OrderReport, OrderType

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def valid_entry() -> EntryOrder:
    return EntryOrder(
        client_order_id="entry-1",
        symbol="BTC/USDT",
        type=OrderType.LIMIT,
        quantity=Decimal(1),
        price=Decimal(100),
        stop_loss=Decimal(90),
        take_profit=Decimal(120),
        created_at=NOW,
    )


def valid_exit() -> ExitOrder:
    return ExitOrder(
        client_order_id="exit-1",
        symbol="BTC/USDT",
        type=OrderType.LIMIT,
        quantity=Decimal(1),
        price=Decimal(110),
        created_at=NOW,
    )


def tampered(order: Order, **changes: Any) -> Order:
    for field, value in changes.items():
        object.__setattr__(order, field, value)
    return order


class ExchangeGatewayContract:
    """Contrato que todo `ExchangeGateway` deve cumprir; herde e implemente os ganchos.

    Ganchos obrigatorios:
    - `gateway` (fixture): instancia nova, sem rede real, que registra o que seria enviado;
    - `sent_orders(gateway)`: ordens que efetivamente chegaram a corretora (ou simulacao);
    - `revalidate_module`: modulo do gateway onde o nome `revalidate_order` e resolvido
      (usado para espionar a chamada).

    O nome da classe herdeira deve comecar com `Test` para o pytest coleta-la.
    """

    revalidate_module: ModuleType

    @pytest.fixture
    def gateway(self) -> ExchangeGateway:
        raise NotImplementedError

    def sent_orders(self, gateway: ExchangeGateway) -> list[Order]:
        raise NotImplementedError

    def entry_submission(self, **changes: Any) -> tuple[Any, Order]:
        """(o que se entrega a `place_order`, a ordem que deve chegar a corretora), com adulteracao opcional.

        Gateways que exigem entrada aprovada pelo gate (Live) sobrescrevem para embrulhar em `ApprovedEntry`.
        """
        order = tampered(valid_entry(), **changes)
        return order, order

    async def test_place_order_sends_valid_orders_unchanged(self, gateway: ExchangeGateway) -> None:
        submission, entry = self.entry_submission()
        exit_ = valid_exit()

        reports = [await gateway.place_order(submission), await gateway.place_order(exit_)]

        assert all(isinstance(report, OrderReport) for report in reports)
        assert [r.client_order_id for r in reports] == [entry.client_order_id, exit_.client_order_id]
        assert self.sent_orders(gateway) == [entry, exit_]

    async def test_place_order_calls_revalidate_order(
        self, gateway: ExchangeGateway, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        original = self.revalidate_module.revalidate_order
        seen: list[Order] = []

        def spy(order: Any) -> Any:
            seen.append(order)
            return original(order)

        monkeypatch.setattr(self.revalidate_module, "revalidate_order", spy)
        submission, order = self.entry_submission()

        await gateway.place_order(submission)

        assert seen == [order]

    @pytest.mark.parametrize(
        "changes",
        [
            {"stop_loss": Decimal(200)},
            {"stop_loss": Decimal(0)},
            {"take_profit": Decimal(101)},
            {"price": Decimal("NaN")},
            {"quantity": Decimal(-1)},
        ],
    )
    async def test_place_order_refuses_tampered_entry_order(
        self, gateway: ExchangeGateway, changes: dict[str, Any]
    ) -> None:
        submission, _ = self.entry_submission(**changes)

        with pytest.raises(ValidationError):
            await gateway.place_order(submission)

        assert self.sent_orders(gateway) == []

    async def test_place_order_refuses_tampered_exit_order(self, gateway: ExchangeGateway) -> None:
        with pytest.raises(ValidationError):
            await gateway.place_order(tampered(valid_exit(), price=None))

        assert self.sent_orders(gateway) == []
