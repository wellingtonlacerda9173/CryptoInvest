from decimal import Decimal as D

import pytest

from engine.domain.candle import Candle
from engine.domain.order import OrderStatus, OrderType
from engine.infrastructure.backtest import BacktestGateway, FillKind, UnknownOrderError
from tests.unit.backtest.test_backtest_gateway import SYMBOL, candle, entry, flat, make, sell


class TestDropTarget:
    async def test_oco_without_target_only_exits_by_stop(self) -> None:
        gw = make([*flat(2), candle(2, "100", "130", "99", "125"), candle(3, "125", "126", "90", "91")])
        await gw.place_order(entry(stop="95", target="110"))
        gw.advance()
        gw.advance()
        gw.drop_target("e1")
        gw.advance()
        assert [f.kind for f in gw.fills] == [FillKind.ENTRY_MARKET]
        gw.raise_stop("e1", D(120))
        gw.advance()
        assert gw.fills[-1].kind is FillKind.STOP_LOSS
        assert gw.fills[-1].price == D(120)

    def test_unknown_entry(self) -> None:
        with pytest.raises(UnknownOrderError):
            make(flat(2)).drop_target("nope")


class TestStopFirstOnManualLimitExit:
    async def _setup(self, last: Candle) -> BacktestGateway:
        gw = make([*flat(2), last])
        await gw.place_order(entry(qty="2", stop="95", target="200"))
        gw.advance()
        gw.advance()
        await gw.place_order(sell(kind=OrderType.LIMIT, qty="1", price="110"))
        return gw

    async def test_limit_sell_is_not_filled_on_a_candle_that_also_touches_the_stop(self) -> None:
        gw = await self._setup(candle(2, "100", "112", "94", "100"))
        gw.advance()
        assert [f.kind for f in gw.fills] == [FillKind.ENTRY_MARKET, FillKind.STOP_LOSS]
        assert gw.fills[-1].quantity == 2
        report = await gw.get_order(SYMBOL, "x1")
        assert report.status is OrderStatus.NEW

    async def test_limit_sell_still_fills_when_the_stop_is_not_touched(self) -> None:
        gw = await self._setup(candle(2, "100", "112", "96", "100"))
        gw.advance()
        assert gw.fills[-1].kind is FillKind.EXIT_LIMIT
