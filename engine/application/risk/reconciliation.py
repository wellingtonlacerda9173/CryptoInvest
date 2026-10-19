from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import Field

from engine.application.risk.position import ManagedPosition
from engine.application.risk.state import RiskState
from engine.domain.base import DomainModel
from engine.domain.market import Balance
from engine.domain.risk import RiskConfig
from engine.domain.types import Price, Quantity, Symbol

STABLE_ASSETS = frozenset({"USDT", "USDC", "FDUSD", "BUSD"})
DEFAULT_TOLERANCE = Decimal("0.001")
DEFAULT_DUST_QUOTE_VALUE = Decimal(1)


class DivergenceKind(StrEnum):
    MISSING_ON_EXCHANGE = "missing_on_exchange"
    UNTRACKED_BALANCE = "untracked_balance"
    UNPROTECTED_POSITION = "unprotected_position"
    PARTIALLY_PROTECTED = "partially_protected"
    STOP_BELOW_EXPECTED = "stop_below_expected"


class Divergence(DomainModel):
    kind: DivergenceKind
    subject: str = Field(min_length=1)
    expected: Decimal
    actual: Decimal
    detail: str = Field(min_length=1)


class ProtectiveStop(DomainModel):
    """Stop protetivo ativo na corretora (perna de stop da OCO)."""

    symbol: Symbol
    quantity: Quantity
    stop_price: Price


class AccountSnapshot(DomainModel):
    """Retrato da corretora: saldos, stops protetivos ativos e preco em quote dos ativos (valora poeira)."""

    balances: Mapping[str, Balance]
    stops: tuple[ProtectiveStop, ...] = ()
    prices: Mapping[str, Price] = Field(default_factory=dict)


def ignored_assets_for(config: RiskConfig) -> frozenset[str]:
    """Ativos fora da reconciliacao: stablecoins e a moeda de cotacao sempre; o ativo de taxas
    (`config.fee_asset`) a menos que o robo o opere de fato, isto e, esteja na whitelist `allowed_assets`
    e fora de `blocked_assets`. Sobrescrever `blocked_assets` sem o ativo de taxas nao o torna operado."""
    ignored = STABLE_ASSETS | {config.quote_asset}
    operated = config.fee_asset in config.allowed_assets and config.fee_asset not in config.blocked_assets
    if not operated:
        ignored |= {config.fee_asset}
    return ignored


def _base(symbol: str) -> str:
    return symbol.split("/")[0]


def _is_dust(asset: str, excess: Decimal, snapshot: AccountSnapshot, dust_quote_value: Decimal) -> bool:
    """Sem preco o valor e desconhecido: nao se presume poeira."""
    price = snapshot.prices.get(asset)
    return price is not None and excess * price < dust_quote_value


def _balance_divergences(
    local: Sequence[ManagedPosition],
    snapshot: AccountSnapshot,
    tolerance: Decimal,
    ignored_assets: frozenset[str],
    dust_quote_value: Decimal,
) -> list[Divergence]:
    expected: dict[str, Decimal] = {}
    for position in local:
        expected[_base(position.symbol)] = (
            expected.get(_base(position.symbol), Decimal(0)) + position.quantity
        )
    actual = {
        asset: b.free + b.locked
        for asset, b in snapshot.balances.items()
        if asset not in ignored_assets or asset in expected
    }
    found: list[Divergence] = []
    for asset in sorted(expected.keys() | actual.keys()):
        want, have = expected.get(asset, Decimal(0)), actual.get(asset, Decimal(0))
        if have < want * (1 - tolerance):
            kind = DivergenceKind.MISSING_ON_EXCHANGE
        elif have > want * (1 + tolerance) and have > 0:
            if _is_dust(asset, have - want, snapshot, dust_quote_value):
                continue
            kind = DivergenceKind.UNTRACKED_BALANCE
        else:
            continue
        found.append(
            Divergence(
                kind=kind,
                subject=asset,
                expected=want,
                actual=have,
                detail=f"{asset}: local {want} x corretora {have}",
            )
        )
    return found


def _protection_divergence(
    position: ManagedPosition, snapshot: AccountSnapshot, tolerance: Decimal
) -> Divergence | None:
    stops = [s for s in snapshot.stops if s.symbol == position.symbol]
    covered = sum((s.quantity for s in stops), start=Decimal(0))
    if not stops:
        return Divergence(
            kind=DivergenceKind.UNPROTECTED_POSITION,
            subject=position.symbol,
            expected=position.stop,
            actual=Decimal(0),
            detail=f"{position.symbol}: posicao sem stop ativo na corretora",
        )
    if covered < position.quantity * (1 - tolerance):
        return Divergence(
            kind=DivergenceKind.PARTIALLY_PROTECTED,
            subject=position.symbol,
            expected=position.quantity,
            actual=covered,
            detail=f"{position.symbol}: stop cobre {covered} de {position.quantity}",
        )
    if (lowest := min(s.stop_price for s in stops)) < position.stop:
        return Divergence(
            kind=DivergenceKind.STOP_BELOW_EXPECTED,
            subject=position.symbol,
            expected=position.stop,
            actual=lowest,
            detail=f"{position.symbol}: stop na corretora {lowest} abaixo do esperado {position.stop}",
        )
    return None


def reconcile(
    local: Sequence[ManagedPosition],
    snapshot: AccountSnapshot,
    *,
    tolerance: Decimal = DEFAULT_TOLERANCE,
    ignored_assets: frozenset[str] | None = None,
    config: RiskConfig | None = None,
    dust_quote_value: Decimal = DEFAULT_DUST_QUOTE_VALUE,
) -> tuple[Divergence, ...]:
    """`ignored_assets` explicito vence; senao deriva-se de `config` (padrao `RiskConfig()`) via
    `ignored_assets_for`. Ativo com posicao local nunca e ignorado."""
    if ignored_assets is None:
        ignored_assets = ignored_assets_for(config or RiskConfig())
    found = _balance_divergences(local, snapshot, tolerance, ignored_assets, dust_quote_value)
    found.extend(d for p in local if (d := _protection_divergence(p, snapshot, tolerance)) is not None)
    return tuple(found)


def pause_on_divergence(state: RiskState, divergences: Sequence[Divergence], now: datetime) -> RiskState:
    if not divergences:
        return state
    return state.pause("reconciliacao: " + "; ".join(d.detail for d in divergences), now)
