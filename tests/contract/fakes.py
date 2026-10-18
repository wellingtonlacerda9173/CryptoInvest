from engine.domain.order import Order, OrderReport, OrderStatus, revalidate_order


class FakeExchangeGateway:
    """Gateway de referencia: so a parte de ordens do contrato `ExchangeGateway`."""

    def __init__(self) -> None:
        self.sent: list[Order] = []

    async def place_order(self, order: Order) -> OrderReport:
        checked = revalidate_order(order)
        self.sent.append(checked)
        return OrderReport(
            order_id=f"ex-{len(self.sent)}",
            client_order_id=checked.client_order_id,
            symbol=checked.symbol,
            status=OrderStatus.NEW,
            filled_quantity=0,
        )


class UnsafeExchangeGateway(FakeExchangeGateway):
    """Gateway defeituoso: nao revalida a ordem. Usado para provar que o contrato o reprova."""

    async def place_order(self, order: Order) -> OrderReport:
        self.sent.append(order)
        return OrderReport(
            order_id="x",
            client_order_id=order.client_order_id,
            symbol=order.symbol,
            status=OrderStatus.NEW,
            filled_quantity=0,
        )
