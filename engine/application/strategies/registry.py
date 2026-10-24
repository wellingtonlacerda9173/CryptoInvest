from collections.abc import Callable, Mapping
from types import MappingProxyType

from engine.application.strategies.base import Strategy
from engine.application.strategies.breakout import DonchianBreakout, RangeBreakout
from engine.application.strategies.elephant_bar_breakout import ElephantBarBreakout
from engine.application.strategies.ema_cross_trend import EmaCrossTrend
from engine.application.strategies.mean_reversion import BollingerRsiReversion
from engine.application.strategies.momentum import MacdCross, RsiBullishDivergence
from engine.application.strategies.price_action import BullishEngulfing, BullishPinBar, SupportRetest
from engine.application.strategies.trend_following import SupertrendFlip
from engine.application.strategies.vwap import VwapReclaim
from engine.domain.types import Timeframe

_FACTORIES: tuple[Callable[[], Strategy], ...] = (
    ElephantBarBreakout,
    EmaCrossTrend,
    BullishEngulfing,
    BullishPinBar,
    SupportRetest,
    SupertrendFlip,
    DonchianBreakout,
    RangeBreakout,
    BollingerRsiReversion,
    MacdCross,
    RsiBullishDivergence,
    VwapReclaim,
)

STRATEGY_REGISTRY: Mapping[str, Callable[[], Strategy]] = MappingProxyType(
    {factory().name: factory for factory in _FACTORIES}
)


def is_applicable(name: str, timeframe: Timeframe) -> bool:
    """False quando a estrategia nunca pode sinalizar no timeframe (comparacao impossivel)."""
    supports = getattr(STRATEGY_REGISTRY[name](), "supports", None)
    return True if supports is None else bool(supports(timeframe))
