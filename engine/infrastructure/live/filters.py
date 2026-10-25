import time
from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Any, Literal, Self

from engine.application.risk.sizing import SymbolRules, ceil_to_step, floor_to_step
from engine.domain.base import DomainModel
from engine.domain.types import Symbol
from engine.infrastructure.live.errors import FilterViolationError, SymbolNotAllowedError

Side = Literal["BUY", "SELL"]
_ZERO = Decimal(0)


class PercentPrice(DomainModel):
    bid_up: Decimal
    bid_down: Decimal
    ask_up: Decimal
    ask_down: Decimal


class SymbolFilters(DomainModel):
    """Filtros de /api/v3/exchangeInfo de um simbolo, mais as permissoes de tipos de ordem."""

    symbol: Symbol
    trading: bool
    spot_allowed: bool
    margin_allowed: bool
    order_types: frozenset[str]
    oco_allowed: bool
    opo_allowed: bool
    tick_size: Decimal
    min_price: Decimal
    max_price: Decimal
    step_size: Decimal
    min_qty: Decimal
    max_qty: Decimal
    min_notional: Decimal
    max_notional: Decimal | None
    percent_price: PercentPrice | None
    market_min_qty: Decimal = _ZERO
    market_max_qty: Decimal = _ZERO
    market_step: Decimal = _ZERO

    @classmethod
    def from_exchange_info(cls, entry: Mapping[str, Any]) -> Self:
        by_type = {f["filterType"]: f for f in entry["filters"]}
        price, lot = by_type["PRICE_FILTER"], by_type["LOT_SIZE"]
        notional = by_type.get("NOTIONAL") or by_type.get("MIN_NOTIONAL") or {}
        pct = by_type.get("PERCENT_PRICE_BY_SIDE")
        market_lot = by_type.get("MARKET_LOT_SIZE", {})
        max_notional = notional.get("maxNotional")
        base, quote = entry["baseAsset"], entry["quoteAsset"]
        return cls(
            symbol=f"{base}/{quote}",
            trading=entry.get("status") == "TRADING",
            spot_allowed=bool(entry.get("isSpotTradingAllowed", False)),
            margin_allowed=bool(entry.get("isMarginTradingAllowed", False)),
            order_types=frozenset(entry.get("orderTypes", [])),
            oco_allowed=bool(entry.get("ocoAllowed", False)),
            opo_allowed=bool(entry.get("opoAllowed", False)),
            tick_size=Decimal(price["tickSize"]),
            min_price=Decimal(price["minPrice"]),
            max_price=Decimal(price["maxPrice"]),
            step_size=Decimal(lot["stepSize"]),
            min_qty=Decimal(lot["minQty"]),
            max_qty=Decimal(lot["maxQty"]),
            min_notional=Decimal(notional.get("minNotional", "0")),
            max_notional=Decimal(max_notional) if max_notional is not None else None,
            market_min_qty=Decimal(market_lot.get("minQty", "0")),
            market_max_qty=Decimal(market_lot.get("maxQty", "0")),
            market_step=Decimal(market_lot.get("stepSize", "0")),
            percent_price=(
                PercentPrice(
                    bid_up=Decimal(pct["bidMultiplierUp"]),
                    bid_down=Decimal(pct["bidMultiplierDown"]),
                    ask_up=Decimal(pct["askMultiplierUp"]),
                    ask_down=Decimal(pct["askMultiplierDown"]),
                )
                if pct
                else None
            ),
        )

    def as_symbol_rules(self) -> SymbolRules:
        return SymbolRules(
            symbol=self.symbol,
            tick_size=self.tick_size,
            step_size=self.step_size,
            min_quantity=self.min_qty,
            max_quantity=self.max_qty if self.max_qty > 0 else None,
            min_notional=self.min_notional,
        )

    def round_quantity_down(self, quantity: Decimal) -> Decimal:
        return floor_to_step(quantity, self.step_size)

    def round_market_quantity_down(self, quantity: Decimal) -> Decimal:
        """Arredonda para baixo respeitando LOT_SIZE e MARKET_LOT_SIZE (passo de ambos; 0 = sem restricao)."""
        value = self.round_quantity_down(quantity)
        for _ in range(8):
            stepped = floor_to_step(value, self.market_step) if self.market_step > 0 else value
            stepped = self.round_quantity_down(stepped)
            if stepped == value:
                break
            value = stepped
        return value

    def market_chunk(self, quantity: Decimal) -> Decimal:
        """Maior quantidade enviavel numa unica ordem a mercado (respeita MARKET_LOT_SIZE.maxQty)."""
        capped = min(quantity, self.market_max_qty) if self.market_max_qty > 0 else quantity
        return self.round_market_quantity_down(capped)

    @property
    def market_min(self) -> Decimal:
        return max(self.min_qty, self.market_min_qty)

    def market_problems(self, quantity: Decimal) -> list[str]:
        found: list[str] = []
        if self.market_min_qty > 0 and quantity < self.market_min_qty:
            found.append(f"MARKET_LOT_SIZE: quantidade {quantity} abaixo do minimo {self.market_min_qty}")
        if self.market_max_qty > 0 and quantity > self.market_max_qty:
            found.append(f"MARKET_LOT_SIZE: quantidade {quantity} acima do maximo {self.market_max_qty}")
        if self.market_step > 0 and quantity % self.market_step != 0:
            found.append(f"MARKET_LOT_SIZE: quantidade {quantity} fora do passo {self.market_step}")
        return found

    def round_price_down(self, price: Decimal) -> Decimal:
        return floor_to_step(price, self.tick_size)

    def round_price_up(self, price: Decimal) -> Decimal:
        return ceil_to_step(price, self.tick_size)

    def problems(
        self, *, quantity: Decimal, price: Decimal | None, side: Side, avg_price: Decimal | None
    ) -> list[str]:
        """Violacoes de LOT_SIZE, PRICE_FILTER, NOTIONAL e PERCENT_PRICE_BY_SIDE (lista vazia = valida)."""
        found: list[str] = []
        if not self.trading or not self.spot_allowed:
            found.append(f"{self.symbol} nao negociavel em spot")
        if quantity <= 0 or quantity < self.min_qty or (self.max_qty > 0 and quantity > self.max_qty):
            found.append(f"LOT_SIZE: quantidade {quantity} fora de [{self.min_qty}, {self.max_qty}]")
        elif not _aligned(quantity - self.min_qty, self.step_size):
            found.append(f"LOT_SIZE: quantidade {quantity} fora do passo {self.step_size}")
        if price is None:
            return found
        if price <= 0 or price < self.min_price or (self.max_price > 0 and price > self.max_price):
            found.append(f"PRICE_FILTER: preco {price} fora de [{self.min_price}, {self.max_price}]")
        elif not _aligned(price - self.min_price, self.tick_size):
            found.append(f"PRICE_FILTER: preco {price} fora do tick {self.tick_size}")
        if price * quantity < self.min_notional:
            found.append(f"NOTIONAL: {price * quantity} abaixo do minimo {self.min_notional}")
        if self.max_notional is not None and price * quantity > self.max_notional:
            found.append(f"NOTIONAL: {price * quantity} acima do maximo {self.max_notional}")
        if self.percent_price is not None and avg_price is not None:
            up, down = (
                (self.percent_price.bid_up, self.percent_price.bid_down)
                if side == "BUY"
                else (self.percent_price.ask_up, self.percent_price.ask_down)
            )
            if not avg_price * down <= price <= avg_price * up:
                found.append(
                    f"PERCENT_PRICE_BY_SIDE: preco {price} fora de {avg_price * down}..{avg_price * up}"
                )
        return found

    def require_valid(
        self, *, quantity: Decimal, price: Decimal | None, side: Side, avg_price: Decimal | None
    ) -> None:
        if found := self.problems(quantity=quantity, price=price, side=side, avg_price=avg_price):
            raise FilterViolationError("; ".join(found))


def _aligned(value: Decimal, step: Decimal) -> bool:
    return step > 0 and value >= 0 and value % step == _ZERO


class FilterCache:
    """exchangeInfo com TTL. So simbolos da lista permitida sao entregues; os demais falham fechado."""

    def __init__(
        self,
        loader: Callable[[], Any],
        allowed: frozenset[Symbol],
        ttl_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._loader = loader
        self._allowed = allowed
        self._ttl = ttl_seconds
        self._clock = clock
        self._loaded_at: float | None = None
        self._filters: dict[Symbol, SymbolFilters] = {}

    def check_allowed(self, symbol: Symbol) -> None:
        if symbol not in self._allowed:
            raise SymbolNotAllowedError(f"{symbol} fora da lista permitida")

    def stale(self, symbol: Symbol) -> SymbolFilters | None:
        """Ultimo filtro conhecido, mesmo vencido (usado quando a venda de emergencia nao pode esperar)."""
        return self._filters.get(symbol)

    async def get(self, symbol: Symbol) -> SymbolFilters:
        self.check_allowed(symbol)
        if self._loaded_at is None or self._clock() - self._loaded_at > self._ttl:
            await self.refresh()
        try:
            return self._filters[symbol]
        except KeyError:
            raise SymbolNotAllowedError(f"{symbol} ausente no exchangeInfo") from None

    async def refresh(self) -> None:
        payload = await self._loader()
        parsed: dict[Symbol, SymbolFilters] = {}
        for entry in payload["symbols"]:
            symbol = f"{entry['baseAsset']}/{entry['quoteAsset']}"
            if symbol in self._allowed:
                parsed[symbol] = SymbolFilters.from_exchange_info(entry)
        self._filters, self._loaded_at = parsed, self._clock()
