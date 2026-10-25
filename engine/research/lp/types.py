from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

Kind = Literal["hodl", "v2", "v3_fixed", "v3_rebal"]
Category = Literal["estavel", "volatil_maior", "volatil_media"]


@dataclass(frozen=True, slots=True)
class Day:
    """Um dia de dados de um pool. Precos em unidades do token 'quote' por 1 token 'base'."""

    day: date
    low: float
    high: float
    close: float
    quote_usd: float
    volume_usd: float
    tvl_usd: float


@dataclass(frozen=True, slots=True)
class PoolSeries:
    pool_id: str
    chain: str
    address: str
    name: str
    fee: float
    category: Category
    days: tuple[Day, ...]
    tvl_start_usd: float
    tvl_end_usd: float

    @property
    def tvl_ratio(self) -> float:
        return self.tvl_end_usd / self.tvl_start_usd if self.tvl_start_usd > 0 else 0.0


@dataclass(frozen=True, slots=True)
class Strategy:
    kind: Kind
    half_width: float = 0.0
    rho: float = 0.5
    v2_kappa: float = 1.0

    @property
    def label(self) -> str:
        match self.kind:
            case "hodl":
                return "HODL"
            case "v2":
                return (
                    "LP v2 (faixa total)"
                    if self.v2_kappa == 1.0
                    else f"faixa total em pool v3 (kappa={self.v2_kappa:g})"
                )
            case "v3_fixed":
                return f"LP v3 fixa +-{self.half_width * 100:g}%"
            case "v3_rebal":
                return f"LP v3 rebalanceada +-{self.half_width * 100:g}%"


@dataclass(frozen=True, slots=True)
class CostModel:
    gas_open_usd: float
    gas_close_usd: float
    gas_swap_usd: float
    swap_cost_bps: float


@dataclass(frozen=True, slots=True)
class SimResult:
    capital_usd: float
    hodl_usd: float
    position_usd: float
    fees_usd: float
    gas_usd: float
    swap_usd: float
    rebalances: int
    time_in_range: float

    @property
    def net_usd(self) -> float:
        return self.position_usd + self.fees_usd - self.gas_usd - self.swap_usd

    @property
    def excess_pct(self) -> float:
        return (self.net_usd - self.hodl_usd) / self.capital_usd

    @property
    def price_effect_pct(self) -> float:
        return (self.position_usd - self.hodl_usd) / self.capital_usd
