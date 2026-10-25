from __future__ import annotations

from dataclasses import dataclass, replace

from engine.research.lp.types import CostModel

GAS_UNITS_OPEN = 500_000
GAS_UNITS_CLOSE = 250_000
GAS_UNITS_SWAP = 150_000


@dataclass(frozen=True, slots=True)
class GasProfile:
    gas_price_gwei: float
    native_usd: float
    extra_usd_per_tx: float = 0.0

    def tx_usd(self, units: int) -> float:
        return units * self.gas_price_gwei * 1e-9 * self.native_usd + self.extra_usd_per_tx


# Premissas de estudo (nao medidas aqui): L1 usa preco de gas conservador (acima do tipico recente);
# L2 soma uma tarifa fixa para dados publicados no L1.
DEFAULT_GAS: dict[str, GasProfile] = {
    "Ethereum": GasProfile(gas_price_gwei=3.0, native_usd=2500.0),
    "Arbitrum": GasProfile(gas_price_gwei=0.05, native_usd=2500.0, extra_usd_per_tx=0.03),
    "Base": GasProfile(gas_price_gwei=0.02, native_usd=2500.0, extra_usd_per_tx=0.03),
    "OP Mainnet": GasProfile(gas_price_gwei=0.02, native_usd=2500.0, extra_usd_per_tx=0.03),
    "Unichain": GasProfile(gas_price_gwei=0.02, native_usd=2500.0, extra_usd_per_tx=0.03),
    "Polygon": GasProfile(gas_price_gwei=60.0, native_usd=0.25, extra_usd_per_tx=0.0),
}


def cost_model(profile: GasProfile, swap_cost_bps: float = 10.0, gas_scale: float = 1.0) -> CostModel:
    scaled = replace(
        profile,
        gas_price_gwei=profile.gas_price_gwei * gas_scale,
        extra_usd_per_tx=profile.extra_usd_per_tx * gas_scale,
    )
    return CostModel(
        gas_open_usd=scaled.tx_usd(GAS_UNITS_OPEN),
        gas_close_usd=scaled.tx_usd(GAS_UNITS_CLOSE),
        gas_swap_usd=scaled.tx_usd(GAS_UNITS_SWAP),
        swap_cost_bps=swap_cost_bps,
    )
