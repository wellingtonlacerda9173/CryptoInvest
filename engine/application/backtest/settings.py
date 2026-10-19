from decimal import Decimal

from pydantic import Field

from engine.application.risk import SymbolRules
from engine.domain.base import DomainModel
from engine.domain.risk import RiskConfig
from engine.domain.types import Fee, StrictDecimal, Volume
from engine.infrastructure.backtest import BacktestConfig

_RULES: dict[str, tuple[str, str, str]] = {
    "BTC/USDT": ("0.01", "0.00001", "5"),
    "ETH/USDT": ("0.01", "0.0001", "5"),
}
_FALLBACK_RULES = ("0.01", "0.001", "5")


def default_rules(symbol: str) -> SymbolRules:
    """Filtros tipicos da Binance Spot (tick/lote/notional); informe os reais para outros pares."""
    tick, step, notional = _RULES.get(symbol, _FALLBACK_RULES)
    return SymbolRules(
        symbol=symbol,
        tick_size=Decimal(tick),
        step_size=Decimal(step),
        min_quantity=Decimal(step),
        min_notional=Decimal(notional),
    )


class BacktestSettings(DomainModel):
    """Parametros do backtest. A taxa taker vive em `risk.taker_fee_rate` (fonte unica de gate, estrategia
    e gateway).

    `history_window` limita as velas entregues a estrategia e ao gate (a janela que o Paper/Live manteria);
    `hypothetical_horizon` e o limite de tempo (em velas) da simulacao das operacoes evitadas.
    """

    initial_capital: Volume = Decimal(10_000)
    maker_fee_rate: Fee = Decimal("0.001")
    slippage_bps: StrictDecimal = Field(default=Decimal(5), ge=0)
    latency_candles: int = Field(default=1, ge=1)
    risk: RiskConfig = RiskConfig()
    history_window: int = Field(default=400, ge=60)
    hypothetical_horizon: int = Field(default=72, ge=1)

    def gateway_config(self) -> BacktestConfig:
        return BacktestConfig(
            initial_quote=self.initial_capital,
            maker_fee=self.maker_fee_rate,
            taker_fee=self.risk.taker_fee_rate,
            slippage_bps=self.slippage_bps,
            latency_candles=self.latency_candles,
        )
