from datetime import UTC, datetime, timedelta
from decimal import Decimal

from engine.application.risk import (
    EventCalendar,
    ManagedPosition,
    MarketSnapshot,
    SymbolRules,
)
from engine.domain.candle import Candle
from engine.domain.signal import Signal
from engine.domain.trade import ExitReason, Trade
from engine.domain.types import Timeframe

NOW = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)
SYMBOL = "BTC/USDT"
D = Decimal


def rules(**kw: object) -> SymbolRules:
    return SymbolRules.model_validate(
        {
            "symbol": SYMBOL,
            "tick_size": D("0.01"),
            "step_size": D("0.001"),
            "min_quantity": D("0.001"),
            "min_notional": D(10),
        }
        | kw
    )


def signal(
    entry: str = "100",
    stop: str = "95",
    target: str = "111",
    symbol: str = SYMBOL,
    created_at: datetime = NOW,
) -> Signal:
    return Signal(
        symbol=symbol,
        timeframe=Timeframe.H1,
        created_at=created_at,
        strategy="t",
        entry=D(entry),
        stop_loss=D(stop),
        take_profit=D(target),
    )


def market(
    bid: str = "99.99",
    ask: str = "100.01",
    volume: str = "5000000",
    symbol: str = SYMBOL,
    at: datetime = NOW,
) -> MarketSnapshot:
    return MarketSnapshot(symbol=symbol, at=at, bid=D(bid), ask=D(ask), quote_volume_24h=D(volume))


def history(count: int = 160, now: datetime = NOW, symbol: str = SYMBOL, drift: float = 0.2) -> list[Candle]:
    """Velas H1 fechadas e contiguas; por padrao uma alta limpa (aprovada pelo filtro de regime) que termina
    em 100: `drift` > 0 sobe, < 0 cai, 0 fica lateral (oscilando em canal)."""
    last_open = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    candles: list[Candle] = []
    for i in range(count):
        back = count - 1 - i
        wave = 0.0 if drift else 1.5 * (-1) ** i
        mid = 100.0 - drift * back + wave
        candles.append(
            Candle(
                symbol=symbol,
                timeframe=Timeframe.H1,
                open_time=last_open - timedelta(hours=back),
                open=D(str(round(mid - drift / 2, 4))),
                high=D(str(round(mid + 0.8, 4))),
                low=D(str(round(mid - 0.8, 4))),
                close=D(str(round(mid, 4))),
                volume=D(10),
                closed=True,
            )
        )
    return candles


def candle(open_: str, high: str, low: str, close: str, at: datetime = NOW) -> Candle:
    return Candle(
        symbol=SYMBOL,
        timeframe=Timeframe.H1,
        open_time=at,
        open=D(open_),
        high=D(high),
        low=D(low),
        close=D(close),
        volume=D(1),
        closed=True,
    )


def position(
    entry: str = "100", stop: str = "95", target: str | None = "110", quantity: str = "1"
) -> ManagedPosition:
    return ManagedPosition(
        symbol=SYMBOL,
        entry_price=D(entry),
        initial_quantity=D(quantity),
        quantity=D(quantity),
        initial_stop=D(stop),
        stop=D(stop),
        take_profit=None if target is None else D(target),
        highest_price=D(entry),
        opened_at=NOW,
    )


def trade(
    pnl_per_unit: str,
    symbol: str = SYMBOL,
    quantity: str = "1",
    at: datetime = NOW,
    trade_id: str | None = None,
) -> Trade:
    return Trade(
        trade_id=trade_id,
        symbol=symbol,
        quantity=D(quantity),
        entry_price=D(1000),
        exit_price=D(1000) + D(pnl_per_unit),
        opened_at=at - timedelta(hours=1),
        closed_at=at,
        exit_reason=ExitReason.STOP_LOSS if D(pnl_per_unit) < 0 else ExitReason.TAKE_PROFIT,
    )


EMPTY_CALENDAR = EventCalendar(
    loaded_at=NOW - timedelta(days=1), valid_until=NOW + timedelta(days=30), acknowledged_no_events=True
)
