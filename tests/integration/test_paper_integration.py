import asyncio
from contextlib import suppress
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from engine.domain.order import EntryOrder, OrderStatus, OrderType
from engine.infrastructure.backtest import FillKind
from engine.infrastructure.binance.market_data import BinanceMarketData
from engine.infrastructure.paper import PaperConfig, PaperGateway

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("binance_reachable")]

SYMBOL = "BTC/USDT"
WS_URL = "wss://stream.binance.com:443"  # a porta 9443 e bloqueada em algumas redes


async def test_paper_gateway_fills_a_simulated_order_against_the_real_book() -> None:
    async with BinanceMarketData(ws_url=WS_URL) as market:
        gateway = PaperGateway(market, config=PaperConfig(latency_ms=200))
        runner = asyncio.create_task(gateway.run())
        try:
            async with asyncio.timeout(30):
                while (prices := gateway.best_prices(SYMBOL)) is None:  # noqa: ASYNC110
                    await asyncio.sleep(0.2)
            ask = prices[1]
            order = EntryOrder(
                client_order_id="paper-it-1",
                symbol=SYMBOL,
                type=OrderType.MARKET,
                quantity=Decimal("0.01"),
                price=ask,
                stop_loss=ask * Decimal("0.99"),
                take_profit=ask * Decimal("1.02"),
                created_at=datetime.now(UTC),
            )
            report = await gateway.place_order(order)
            assert report.status is OrderStatus.NEW
            async with asyncio.timeout(15):
                while not gateway.fills:  # noqa: ASYNC110
                    await asyncio.sleep(0.2)
        finally:
            runner.cancel()
            with suppress(asyncio.CancelledError):
                await runner

    fill = gateway.fills[0]
    assert fill.kind is FillKind.ENTRY_MARKET
    assert fill.price >= ask
    assert gateway.snapshot().ocos[0].status is OrderStatus.NEW
    assert (await gateway.get_balances())["USDT"].free < Decimal(10_000)
