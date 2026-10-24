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


class _NoDrawdownHaltRisk(RiskConfig):
    """So para o `RiskState` do backtest de validacao: o teto de `max_drawdown` sobe para 100% (so o capital
    zerado desliga). Nao e alcancavel pelo RiskConfig de producao/API, que segue limitado a 5%."""

    max_drawdown: StrictDecimal = Field(default=Decimal(1), gt=0, le=1)


class BacktestSettings(DomainModel):
    """Parametros do backtest. A taxa taker vive em `risk.taker_fee_rate` (fonte unica de gate, estrategia
    e gateway).

    `history_window` limita as velas entregues a estrategia e ao gate (a janela que o Paper/Live manteria);
    `hypothetical_horizon` e o limite de tempo (em velas) da simulacao das operacoes evitadas.
    `drawdown_halt=False` (so a coleta de amostras da validacao) tira o halt permanente por drawdown do
    estado de risco; todas as demais regras do gate continuam valendo.
    """

    initial_capital: Volume = Decimal(10_000)
    maker_fee_rate: Fee = Decimal("0.001")
    slippage_bps: StrictDecimal = Field(default=Decimal(5), ge=0)
    latency_candles: int = Field(default=1, ge=1)
    risk: RiskConfig = RiskConfig()
    history_window: int = Field(default=400, ge=60)
    hypothetical_horizon: int = Field(default=72, ge=1)
    drawdown_halt: bool = True

    def state_risk(self) -> RiskConfig:
        """Config lida pelo `RiskState` (halt por drawdown); o gate e o sizing seguem em `risk`."""
        if self.drawdown_halt:
            return self.risk
        return _NoDrawdownHaltRisk.model_validate({**self.risk.model_dump(), "max_drawdown": Decimal(1)})

    def gateway_config(self) -> BacktestConfig:
        return BacktestConfig(
            initial_quote=self.initial_capital,
            maker_fee=self.maker_fee_rate,
            taker_fee=self.risk.taker_fee_rate,
            slippage_bps=self.slippage_bps,
            latency_candles=self.latency_candles,
        )
