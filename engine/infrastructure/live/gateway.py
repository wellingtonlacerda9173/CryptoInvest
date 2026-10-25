import asyncio
import contextlib
import hashlib
import time
import traceback
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

import structlog

from engine.application.risk.approval import ApprovalExpiredError, ApprovedEntry
from engine.application.risk.position import ManagedPosition
from engine.application.risk.reconciliation import (
    AccountSnapshot,
    Divergence,
    ProtectiveStop,
    ignored_assets_for,
    reconcile,
)
from engine.domain.candle import Candle
from engine.domain.gateway import DuplicateOrderError, MarketDataGateway, UnknownOrderError
from engine.domain.market import Balance, OrderBookSnapshot
from engine.domain.order import (
    EntryOrder,
    ExitOrder,
    Order,
    OrderReport,
    OrderStatus,
    OrderType,
    revalidate_order,
)
from engine.domain.risk import RiskConfig
from engine.domain.types import Symbol, Timeframe
from engine.infrastructure.binance.mapping import to_exchange_symbol
from engine.infrastructure.config.logging import scrub_text
from engine.infrastructure.live.client import BinanceRestClient
from engine.infrastructure.live.config import LiveConfig
from engine.infrastructure.live.errors import (
    BinanceApiError,
    ClockDriftError,
    EntryNotApprovedError,
    FilterViolationError,
    InsufficientBalanceError,
    IpBannedError,
    LiveError,
    OrderOutcomeUnknownError,
    OutcomeUnknownError,
    ProtectionError,
    ProtectiveOrderCancelError,
    TradingHaltedError,
    UnattributedSalesError,
)
from engine.infrastructure.live.fills import FillKind, FillLog, LiveFill
from engine.infrastructure.live.filters import FilterCache, SymbolFilters
from engine.infrastructure.live.recovery import (
    OperatorConfirmation,
    OrphanCloseResult,
    PersistedEntry,
    RecoveredEntry,
    RecoveryOutcome,
    RecoveryResult,
)

log = structlog.get_logger()

OCO_SUFFIX = ":oco"
_ZERO = Decimal(0)
_BPS = Decimal(10_000)
_NOT_FOUND = -2013
_UNKNOWN_ORDER = -2011
_REJECTED = -2010
_WOULD_TRIGGER = -2021
_FILTER_FAILURE = -1013
_STATUS = {
    "NEW": OrderStatus.NEW,
    "PENDING_NEW": OrderStatus.NEW,
    "PENDING_CANCEL": OrderStatus.NEW,
    "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
    "FILLED": OrderStatus.FILLED,
    "CANCELED": OrderStatus.CANCELED,
    "REJECTED": OrderStatus.REJECTED,
    "EXPIRED": OrderStatus.EXPIRED,
    "EXPIRED_IN_MATCH": OrderStatus.EXPIRED,
}
_GENERATION_LOOKAHEAD = 2
_MAX_EMERGENCY_PROBES = 200
_DONE = frozenset({"FILLED", "CANCELED", "EXPIRED", "REJECTED", "EXPIRED_IN_MATCH"})

Alert = Callable[[str, str], None]


def derived_id(entry_id: str, tag: str) -> str:
    """Id deterministico (<= 36 chars) por entrada/etapa: mesmo pedido => mesmo id => idempotencia."""
    return f"{tag}-{hashlib.sha256(f'{entry_id}|{tag}'.encode()).hexdigest()[:30]}"


def _base(symbol: str) -> str:
    return symbol.split("/")[0]


def _order_status(raw: dict[str, Any]) -> OrderStatus:
    return _STATUS.get(str(raw.get("status")), OrderStatus.NEW)


@dataclass
class _Protection:
    quantity: Decimal
    stop: Decimal
    target: Decimal | None
    generation: int

    @property
    def kind(self) -> Literal["oco", "stop"]:
        return "oco" if self.target is not None else "stop"


@dataclass(frozen=True)
class _Tracked:
    """Ordem de venda desta entrada que pode ter executado sem o robo saber (perna/saida/emergencia)."""

    kind: FillKind
    generation: int | None


@dataclass
class _Entry:
    """Ledger por entrada: `owed = comprado liquido de comissao - vendido` e a UNICA fonte de quantidade para
    proteger, sair e vender em emergencia. `sold` e indexado por client id (idempotente: max por ordem)."""

    order: EntryOrder
    state: Literal["working", "protected", "exposed", "closed"] = "working"
    generation: int = 0
    protection: _Protection | None = None
    bought_gross: Decimal = _ZERO
    sold: dict[str, Decimal] = field(default_factory=dict)
    tracked: dict[str, _Tracked] = field(default_factory=dict)
    collected: dict[str, Decimal] = field(default_factory=dict)
    final: set[str] = field(default_factory=set)
    want_stop: Decimal | None = None
    want_target: Decimal | None = None
    expires_at: datetime | None = None
    em_seq: int = 0
    placed_at: float = 0.0
    partial_since: float | None = None
    filled_at: float | None = None
    exposed_since: float | None = None
    exposure_alerted: bool = False
    poll_failures: int = 0
    post_attempt_at: float | None = None
    submitted: bool = False
    buy_open: bool = False
    unknown_since: float | None = None
    emergency_alerted_at: float | None = None
    rebuilt: bool = True
    recover: PersistedEntry | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    closed_reason: str = ""

    @property
    def entry_id(self) -> str:
        return self.order.client_order_id

    @property
    def symbol(self) -> Symbol:
        return self.order.symbol


@dataclass(frozen=True)
class ReconciliationResult:
    divergences: tuple[Divergence, ...]
    unknown_open_orders: tuple[str, ...]
    unknown_trades: tuple[int, ...]
    protection_mismatches: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not (
            self.divergences or self.unknown_open_orders or self.unknown_trades or self.protection_mismatches
        )

    def describe(self) -> str:
        parts = [d.detail for d in self.divergences]
        parts += [f"ordem desconhecida na corretora: {o}" for o in self.unknown_open_orders]
        parts += [f"trade desconhecido: {t}" for t in self.unknown_trades]
        parts += list(self.protection_mismatches)
        return "; ".join(parts)


class LiveGateway:
    """Gateway Binance Spot REST assinado. Invariante central: nenhuma posicao fica sem stop na corretora.

    Estado `exposed` = ha base em carteira sem stop ativo. Toda entrada em `exposed` termina em: protegida
    (`protected`), vendida (`closed`) ou gateway PAUSADO + alerta critico com retomada segura (o proximo
    `poll`/operacao tenta reproteger/vender o exposto ANTES de qualquer outra coisa; novas entradas ficam
    bloqueadas enquanto houver exposicao).

    Entrada = OPOCO (`/api/v3/orderList/opoco`): compra LIMIT + OCO de venda (alvo LIMIT_MAKER e stop) criada
    atomicamente; a corretora define a quantidade das pernas pela quantidade recebida LIQUIDA de comissao.
    Se a OCO nao ficar ativa apos o fill (ou no fill parcial), `poll` coloca uma OCO avulsa e, falhando apos
    os retries, vende a mercado (emergencia). Stops so sobem (`raise_stop`); nenhum caminho cancela protecao
    exceto raise_stop/drop_target (com recolocacao imediata), venda executada e emergencia.
    """

    def __init__(
        self,
        config: LiveConfig,
        client: BinanceRestClient,
        market: MarketDataGateway | None = None,
        *,
        risk_config: RiskConfig | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Any] = asyncio.sleep,
        on_alert: Alert | None = None,
        on_fill: Callable[[LiveFill], None] | None = None,
        on_audit: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._config = config
        self._client = client
        self._market = market
        self._risk = risk_config or RiskConfig()
        self._now = now
        self._mono = monotonic
        self._sleep = sleep
        self._alert_cb = on_alert
        self._audit_cb = on_audit
        self._fills = FillLog(
            on_fill,
            lambda n: self._alert("warning", f"sink de fills falhou; {n} fill(s) pendentes na outbox"),
        )
        self._filters = FilterCache(
            self._load_exchange_info, config.allowed_symbols, config.filters_ttl_seconds
        )
        self._entries: dict[str, _Entry] = {}
        client.on_drift = lambda offset: self._alert(
            "warning", f"relogio local desviado {offset}ms: assinatura compensada em chamada critica"
        )
        self._submitted: list[Order] = []
        self._halt_reason: str | None = None
        self._started_ms = int(now().timestamp() * 1000)

    # ---- estado/observabilidade -------------------------------------------------------------------

    @property
    def halted(self) -> bool:
        return self._halt_reason is not None

    @property
    def halt_reason(self) -> str | None:
        return self._halt_reason

    def resume(self) -> None:
        """Retoma novas entradas: somente por decisao explicita de um operador."""
        self._halt_reason = None

    def exposed_entry_ids(self) -> list[str]:
        return [e.entry_id for e in self._entries.values() if e.state == "exposed"]

    def fills(self) -> tuple[LiveFill, ...]:
        return self._fills.all()

    def submitted_orders(self) -> list[Order]:
        return list(self._submitted)

    def protection_of(self, entry_id: str) -> tuple[Decimal, Decimal, Decimal | None] | None:
        entry = self._entries.get(entry_id)
        if entry is None or entry.protection is None or entry.state != "protected":
            return None
        p = entry.protection
        return p.quantity, p.stop, p.target

    def protection_ids(self, entry_id: str) -> tuple[str, ...]:
        """Ids de cliente das ordens protetivas da geracao vigente (para testes e auditoria)."""
        entry = self._entry(entry_id)
        generation = entry.protection.generation if entry.protection else entry.generation
        return tuple(self._ids(entry_id, generation).values())

    def entry_state(self, entry_id: str) -> str:
        return self._entry(entry_id).state

    def open_entry_ids(self) -> list[str]:
        return [e.entry_id for e in self._entries.values() if e.state != "closed"]

    # ---- MarketDataGateway (delegado) -------------------------------------------------------------

    async def fetch_candles(
        self, symbol: Symbol, timeframe: Timeframe, start: datetime, end: datetime | None = None
    ) -> list[Candle]:
        return await self._require_market().fetch_candles(symbol, timeframe, start, end)

    def stream_candles(
        self, symbol: Symbol, timeframe: Timeframe, since: datetime | None = None
    ) -> AsyncIterator[Candle]:
        return self._require_market().stream_candles(symbol, timeframe, since)

    def stream_order_book(self, symbol: Symbol, depth: int = 10) -> AsyncIterator[OrderBookSnapshot]:
        return self._require_market().stream_order_book(symbol, depth)

    def _require_market(self) -> MarketDataGateway:
        if self._market is None:
            raise LiveError("sem fonte de dados de mercado configurada")
        return self._market

    # ---- saldo ------------------------------------------------------------------------------------

    async def get_balances(self) -> dict[str, Balance]:
        raw = await self._account()
        result: dict[str, Balance] = {}
        for item in raw["balances"]:
            free, locked = Decimal(item["free"]), Decimal(item["locked"])
            if free or locked:
                result[item["asset"]] = Balance(asset=item["asset"], free=free, locked=locked)
        quote = self._config.quote_asset
        real = result.get(quote, Balance(asset=quote, free=_ZERO, locked=_ZERO))
        allowance = max(_ZERO, self._config.capital_cap_quote - self._committed_quote())
        result[quote] = Balance(asset=quote, free=min(real.free, allowance), locked=real.locked)
        return result

    async def equity_quote(self) -> Decimal:
        """Capital operavel: saldo da cotacao + custo das posicoes abertas, limitado pelo teto Live."""
        raw = await self._account()
        quote = next((b for b in raw["balances"] if b["asset"] == self._config.quote_asset), None)
        total = Decimal(quote["free"]) + Decimal(quote["locked"]) if quote else _ZERO
        return min(self._config.capital_cap_quote, total + self._committed_quote())

    def _committed_quote(self) -> Decimal:
        return sum(
            (e.order.quantity * e.order.price for e in self._entries.values() if e.state != "closed"), _ZERO
        )

    async def _account(self, *, critical: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = await self._client.request(
            "GET", "/api/v3/account", signed=True, weight=20, critical=critical
        )
        return data

    async def _free_base(self, symbol: Symbol, *, critical: bool = True) -> Decimal:
        raw = await self._account(critical=critical)
        asset = _base(symbol)
        item = next((b for b in raw["balances"] if b["asset"] == asset), None)
        return Decimal(item["free"]) if item else _ZERO

    # ---- place / cancel / get ---------------------------------------------------------------------

    async def place_order(self, order: Order | ApprovedEntry) -> OrderReport:
        """Entradas so via `ApprovedEntry` (selo do gate). Saidas: MARKET ou LIMIT IOC (sem ordem ociosa)."""
        if isinstance(order, ApprovedEntry):
            checked = revalidate_order(order.order)
            order.verify()
            if order.is_expired(self._now()):
                raise ApprovalExpiredError("approval expired")
            if not isinstance(checked, EntryOrder):
                raise EntryNotApprovedError("approval sem ordem de entrada")
            return await self._place_entry(order, checked)
        checked_order = revalidate_order(order)
        if isinstance(checked_order, EntryOrder):
            raise EntryNotApprovedError("entradas exigem ApprovedEntry emitido pelo gate de risco")
        return await self._place_exit(checked_order)

    async def cancel_order(self, symbol: Symbol, client_order_id: str) -> OrderReport:
        entry = self._entries.get(client_order_id)
        if entry is None:
            if client_order_id.endswith(OCO_SUFFIX) or self._is_protective_id(client_order_id):
                raise ProtectiveOrderCancelError("cancelar OCO/stop protetivo e proibido")
            raise UnknownOrderError(client_order_id)
        async with entry.lock, self._watch(entry):
            if entry.state != "working":
                return await self._entry_report(entry)
            self._ensure_writable()
            try:
                await self._cancel_list(entry.symbol, derived_id(entry.entry_id, "L0"), critical=False)
            except BinanceApiError as error:
                if error.code not in (_UNKNOWN_ORDER, _NOT_FOUND):
                    raise
            await self._advance_working(entry, after_cancel=True)
            return await self._entry_report(entry)

    async def get_order(self, symbol: Symbol, client_order_id: str) -> OrderReport:
        if client_order_id.endswith(OCO_SUFFIX):
            entry = self._entry(client_order_id.removesuffix(OCO_SUFFIX))
            return await self._protection_report(entry)
        tracked = self._entries.get(client_order_id)
        if tracked is not None:
            return await self._entry_report(tracked)
        raw = await self._get_order(symbol, client_order_id)
        if raw is None:
            raise UnknownOrderError(client_order_id)
        return self._report_from(raw, symbol)

    # ---- entrada ----------------------------------------------------------------------------------

    async def _place_entry(self, approved: ApprovedEntry, order: EntryOrder) -> OrderReport:
        self._ensure_writable()
        await self._repair_exposed()
        if self.halted:
            raise TradingHaltedError(self._halt_reason or "halted")
        if self.exposed_entry_ids():
            raise TradingHaltedError("posicao exposta pendente de protecao: novas entradas bloqueadas")
        cid = order.client_order_id
        if cid in self._entries or cid.endswith(OCO_SUFFIX):
            raise DuplicateOrderError(cid)
        filters = await self._filters.get(order.symbol)
        self._check_entry_filters(filters, order)
        avg = await self._avg_price(order.symbol)
        filters.require_valid(quantity=order.quantity, price=order.price, side="BUY", avg_price=avg)
        for level in (order.stop_loss, order.take_profit):
            filters.require_valid(quantity=order.quantity, price=level, side="SELL", avg_price=avg)
        cost = order.quantity * order.price
        # Reserva de capital SEM await entre a conferencia do teto e o registro da entrada: duas place_order
        # concorrentes nao passam ambas pelo teto (o loop de eventos so alterna em await).
        if cid in self._entries:
            raise DuplicateOrderError(cid)
        allowance = max(_ZERO, self._config.capital_cap_quote - self._committed_quote())
        if cost > allowance:
            raise InsufficientBalanceError(f"custo {cost} acima do capital Live disponivel {allowance}")
        entry = _Entry(order=order, placed_at=self._mono(), expires_at=approved.guards.expires_at)
        ids0 = self._ids(cid, 0)
        entry.tracked = {
            ids0["below"]: _Tracked(FillKind.STOP, 0),
            ids0["above"]: _Tracked(FillKind.TARGET, 0),
        }
        self._entries[cid] = entry
        try:
            async with entry.lock, self._watch(entry):
                free_quote = await self._free_quote()
                if cost > free_quote:
                    raise InsufficientBalanceError(f"custo {cost} acima do saldo livre {free_quote}")
                await self._submit_entry(entry, filters)
                self._submitted.append(order)
                await self._advance(entry)
        except BaseException as error:
            await self._discard_unsent_entry(entry, error)
            raise
        return await self._entry_report(entry)

    async def _discard_unsent_entry(self, entry: _Entry, error: BaseException) -> None:
        """Libera reserva de capital e client id quando NENHUMA ordem pode ter chegado a corretora. POST ja
        disparado e sem confirmacao: so descarta se um GET confirmar ausencia; senao a entrada fica `working`
        e `_advance_working` a encerra por TTL (`_expire_unsent`)."""
        if entry.submitted or self._entries.get(entry.entry_id) is not entry:
            return
        if entry.post_attempt_at is None or (
            isinstance(error, LiveError) and not await self._entry_exists(entry)
        ):
            self._entries.pop(entry.entry_id, None)

    def _check_entry_filters(self, filters: SymbolFilters, order: EntryOrder) -> None:
        missing = [t for t in ("LIMIT", "LIMIT_MAKER", "STOP_LOSS") if t not in filters.order_types]
        if self._config.stop_leg == "stop_loss_limit" and "STOP_LOSS_LIMIT" not in filters.order_types:
            missing.append("STOP_LOSS_LIMIT")
        if missing or not filters.oco_allowed or not filters.opo_allowed:
            raise FilterViolationError(f"{order.symbol}: OPOCO/OCO nao permitido ({missing})")
        if order.type is not OrderType.LIMIT:
            raise FilterViolationError("somente entradas LIMIT (OPOCO exige ordem de trabalho limite)")
        if filters.round_price_up(order.stop_loss) != order.stop_loss or (
            filters.round_price_down(order.take_profit) != order.take_profit
        ):
            raise FilterViolationError("stop/alvo fora do tick_size")
        if order.quantity * order.stop_loss < filters.min_notional:
            raise FilterViolationError("notional do stop abaixo do minimo")

    async def _free_quote(self) -> Decimal:
        raw = await self._account()
        item = next((b for b in raw["balances"] if b["asset"] == self._config.quote_asset), None)
        return Decimal(item["free"]) if item else _ZERO

    def _stop_params(self, prefix: str, stop: Decimal, filters: SymbolFilters) -> dict[str, str]:
        if self._config.stop_leg == "stop_loss":
            return {f"{prefix}Type": "STOP_LOSS", f"{prefix}StopPrice": _fmt(stop)}
        limit = filters.round_price_down(stop * (1 - self._config.stop_limit_offset_bps / _BPS))
        if limit < filters.min_price or limit <= 0:
            raise FilterViolationError("preco limite do stop invalido")
        return {
            f"{prefix}Type": "STOP_LOSS_LIMIT",
            f"{prefix}StopPrice": _fmt(stop),
            f"{prefix}Price": _fmt(limit),
            f"{prefix}TimeInForce": "GTC",
        }

    async def _submit_entry(self, entry: _Entry, filters: SymbolFilters) -> None:
        o = entry.order
        ids = self._ids(entry.entry_id, 0)
        params: dict[str, str] = {
            "symbol": to_exchange_symbol(o.symbol),
            "listClientOrderId": ids["list"],
            "workingType": "LIMIT",
            "workingSide": "BUY",
            "workingClientOrderId": o.client_order_id,
            "workingPrice": _fmt(o.price),
            "workingQuantity": _fmt(o.quantity),
            "workingTimeInForce": "GTC",
            "pendingSide": "SELL",
            "pendingAboveType": "LIMIT_MAKER",
            "pendingAboveClientOrderId": ids["above"],
            "pendingAbovePrice": _fmt(o.take_profit),
            "pendingBelowClientOrderId": ids["below"],
            "newOrderRespType": "RESULT",
        }
        params |= self._stop_params("pendingBelow", o.stop_loss, filters)
        await self._post_idempotent(
            "/api/v3/orderList/opoco",
            params,
            lookup=lambda: self._get_list(o.symbol, ids["list"]),
            on_attempt=lambda: setattr(entry, "post_attempt_at", self._mono()),
        )
        entry.submitted = True

    @staticmethod
    def _ids(entry_id: str, generation: int) -> dict[str, str]:
        return {
            "list": derived_id(entry_id, f"L{generation}"),
            "above": derived_id(entry_id, f"T{generation}"),
            "below": derived_id(entry_id, f"S{generation}"),
        }

    def _is_protective_id(self, client_order_id: str) -> bool:
        return any(
            client_order_id in self._ids(e.entry_id, g).values()
            or client_order_id == derived_id(e.entry_id, f"X{g}")
            for e in self._entries.values()
            for g in range(e.generation + 2)
        )

    # ---- envio idempotente ------------------------------------------------------------------------

    async def _post_idempotent(
        self,
        path: str,
        params: dict[str, str],
        *,
        lookup: Callable[[], Any],
        critical: bool = False,
        on_attempt: Callable[[], None] | None = None,
    ) -> Any:
        """POST de ordem. Apos timeout/5xx consulta por id ANTES de reenviar; nunca duplica."""
        for _ in range(self._config.order_lookup_retries):
            if on_attempt is not None:
                on_attempt()
            try:
                return await self._client.request("POST", path, params, signed=True, critical=critical)
            except BinanceApiError as error:
                if error.code == _REJECTED and "duplicate" in error.message.lower():
                    found = await self._lookup_resolved(lookup)
                    if found is not None:
                        return found
                raise
            except OutcomeUnknownError:
                found = await self._lookup_resolved(lookup)
                if found is not None:
                    return found
                await self._sleep(0.05)
        raise OrderOutcomeUnknownError(
            f"{path}: resultado desconhecido apos {self._config.order_lookup_retries} consultas"
        )

    async def _lookup_resolved(self, lookup: Callable[[], Any]) -> Any | None:
        """None = confirmado ausente (seguro reenviar). Falha na consulta = desconhecido (nao reenvia)."""
        for _ in range(self._config.order_lookup_retries):
            try:
                return await lookup()
            except OutcomeUnknownError:
                await self._sleep(0.05)
        raise OrderOutcomeUnknownError("consulta pos-timeout tambem falhou; ordem pendente de resolucao")

    async def _get_order(self, symbol: Symbol, cid: str, *, critical: bool = False) -> dict[str, Any] | None:
        try:
            data: dict[str, Any] = await self._client.request(
                "GET",
                "/api/v3/order",
                {"symbol": to_exchange_symbol(symbol), "origClientOrderId": cid},
                signed=True,
                weight=4,
                critical=critical,
            )
            return data
        except BinanceApiError as error:
            if error.code == _NOT_FOUND:
                return None
            raise

    async def _get_list(
        self, symbol: Symbol, list_id: str, *, critical: bool = False
    ) -> dict[str, Any] | None:
        try:
            data: dict[str, Any] = await self._client.request(
                "GET",
                "/api/v3/orderList",
                {"origClientOrderId": list_id},
                signed=True,
                weight=4,
                critical=critical,
            )
            return data
        except BinanceApiError as error:
            if error.code == _NOT_FOUND:
                return None
            raise

    async def _cancel_list(self, symbol: Symbol, list_id: str, *, critical: bool) -> Any:
        return await self._client.request(
            "DELETE",
            "/api/v3/orderList",
            {"symbol": to_exchange_symbol(symbol), "listClientOrderId": list_id},
            signed=True,
            critical=critical,
        )

    async def _entry_exists(self, entry: _Entry) -> bool:
        try:
            return await self._get_list(entry.symbol, derived_id(entry.entry_id, "L0")) is not None
        except LiveError:
            return True

    async def _avg_price(self, symbol: Symbol) -> Decimal:
        data = await self._client.request(
            "GET", "/api/v3/avgPrice", {"symbol": to_exchange_symbol(symbol)}, weight=2
        )
        return Decimal(data["price"])

    async def _bid(self, symbol: Symbol, *, critical: bool = False) -> Decimal:
        data = await self._client.request(
            "GET",
            "/api/v3/ticker/bookTicker",
            {"symbol": to_exchange_symbol(symbol)},
            weight=2,
            critical=critical,
        )
        return Decimal(data["bidPrice"])

    async def _load_exchange_info(self) -> Any:
        symbols = ",".join(f'"{to_exchange_symbol(s)}"' for s in sorted(self._config.allowed_symbols))
        return await self._client.request(
            "GET", "/api/v3/exchangeInfo", {"symbols": f"[{symbols}]"}, weight=20
        )

    def _ensure_writable(self) -> None:
        from engine.infrastructure.live.errors import ReadOnlyModeError

        if self._config.read_only:
            raise ReadOnlyModeError("modo somente leitura: nenhuma ordem sai")

    # ---- progresso das entradas e protecao --------------------------------------------------------

    async def poll(self) -> None:
        """Um ciclo: retomada segura do exposto primeiro, depois fills/protecao; falha de uma entrada nao
        impede as demais. Erros de protecao sao consolidados e relancados ao fim (apos pausar)."""
        self._fills.flush()
        errors: list[str] = []
        pending = [e for e in self._entries.values() if e.state != "closed"]
        for entry in sorted(pending, key=lambda e: e.state != "exposed"):
            async with entry.lock, self._watch(entry):
                try:
                    await self._advance(entry)
                    entry.poll_failures = 0
                except ProtectionError as error:
                    errors.append(str(error))
                except LiveError as error:
                    log.warning("live_poll_failed", entry=entry.entry_id, error=type(error).__name__)
                    self._halt_if_exposed(entry, error)
                    self._count_poll_failure(entry, error)
                except Exception as error:
                    log.error(
                        "live_poll_unexpected",
                        entry=entry.entry_id,
                        error=type(error).__name__,
                        detail=scrub_text("".join(traceback.format_exception(error))),
                    )
                    errors.append(f"{entry.entry_id}: erro inesperado {type(error).__name__}")
                    self._halt_if_exposed(entry, error)
        if errors:
            if not self.halted:
                self._halt("; ".join(errors))
            raise ProtectionError("; ".join(errors))

    def _count_poll_failure(self, entry: _Entry, error: Exception) -> None:
        """Gateway cego: leituras falham em sequencia sobre uma posicao aberta (ex.: relogio desviado)."""
        entry.poll_failures += 1
        if entry.poll_failures >= self._config.poll_blind_threshold:
            self._halt(
                f"{entry.entry_id}: {entry.poll_failures} ciclos seguidos sem conseguir ler a posicao "
                f"({type(error).__name__}); gateway cego"
            )

    @contextlib.asynccontextmanager
    async def _watch(self, entry: _Entry) -> AsyncIterator[None]:
        """Saida anormal (inclusive CancelledError) com a entrada `exposed`: PAUSA + alerta e relanca."""
        try:
            yield
        except BaseException as error:
            if entry.state == "exposed" and not self.halted:
                self._halt(
                    f"{entry.entry_id}: interrompido ({type(error).__name__}) com a posicao SEM PROTECAO; "
                    "retomada segura pendente"
                )
            raise

    async def run(self) -> None:
        while True:
            try:
                await self.poll()
            except Exception as error:
                log.error("live_poll_cycle_failed", error=type(error).__name__)
            await asyncio.sleep(self._config.poll_interval_seconds)

    def _halt_if_exposed(self, entry: _Entry, error: Exception) -> None:
        if entry.state == "exposed":
            self._halt(
                f"{entry.entry_id}: posicao SEM PROTECAO ({type(error).__name__}); retomada segura pendente"
            )

    async def _repair_exposed(self) -> None:
        """Retomada segura: ANTES de qualquer outra operacao, reprotege/vende todo o exposto."""
        for entry in [e for e in self._entries.values() if e.state == "exposed"]:
            async with entry.lock, self._watch(entry):
                if entry.state != "exposed":
                    continue
                try:
                    await self._advance_exposed(entry)
                except Exception as error:
                    log.warning("live_repair_failed", entry=entry.entry_id, error=type(error).__name__)
                    self._halt_if_exposed(entry, error)

    async def _advance(self, entry: _Entry) -> None:
        if entry.state == "exposed":
            await self._advance_exposed(entry)
        if entry.state == "working":
            await self._advance_working(entry)
        if entry.state == "protected":
            await self._advance_protected(entry)

    async def _advance_exposed(self, entry: _Entry) -> None:
        if not entry.rebuilt:
            await self._rebuild(entry)
            if entry.state != "exposed":
                return
        await self._reprotect(entry, reason="retomada segura")

    # ---- ledger -----------------------------------------------------------------------------------

    def _fee_in_base(self, entry: _Entry) -> Decimal:
        base = _base(entry.symbol)
        return sum(
            (
                f.commission
                for f in self._fills.all()
                if f.order_client_id == entry.entry_id and f.commission_asset == base
            ),
            _ZERO,
        )

    def _sold(self, entry: _Entry) -> Decimal:
        return sum(entry.sold.values(), _ZERO)

    def _owed(self, entry: _Entry) -> Decimal:
        """Quantidade que ESTA entrada ainda detem na corretora: comprado liquido - vendido (nunca < 0)."""
        return max(_ZERO, entry.bought_gross - self._fee_in_base(entry) - self._sold(entry))

    async def _fresh_owed(self, entry: _Entry) -> Decimal:
        """UNICO ponto que entrega `owed` para vender/proteger: confere antes na corretora (`_sync_ledger`).
        Falha na leitura propaga (fail-closed): sem ledger recente nenhuma venda e calculada."""
        await self._sync_ledger(entry)
        return self._owed(entry)

    @staticmethod
    def _record_sale(entry: _Entry, cid: str, executed: Decimal) -> None:
        if executed > entry.sold.get(cid, _ZERO):
            entry.sold[cid] = executed

    @staticmethod
    def _record_reports(entry: _Entry, response: Any) -> bool:
        """Executado das ordens devolvidas por um DELETE (orderReports). True = alguma venda registrada."""
        if not isinstance(response, dict):
            return False
        reports = response.get("orderReports")
        items = reports if isinstance(reports, list) else [response]
        recorded = False
        for item in items:
            cid = str(item.get("clientOrderId", "")) if isinstance(item, dict) else ""
            if cid in entry.tracked:
                executed = Decimal(str(item.get("executedQty", "0")))
                if executed > 0:
                    LiveGateway._record_sale(entry, cid, executed)
                    recorded = True
        return recorded

    async def _sync_ledger(self, entry: _Entry) -> None:
        """Confere na corretora toda ordem de venda desta entrada ainda nao encerrada+registrada: pernas de
        todas as geracoes, saidas e vendas de emergencia. Executado -> ledger (e fills). Chamar ANTES de
        qualquer venda ou recolocacao de protecao."""
        for cid, tracked in list(entry.tracked.items()):
            if cid in entry.final:
                continue
            raw = await self._get_order(entry.symbol, cid, critical=True)
            if raw is None:
                if tracked.generation is not None and tracked.generation < entry.generation:
                    entry.final.add(cid)
                continue
            executed = Decimal(raw["executedQty"])
            if executed > 0:
                self._record_sale(entry, cid, executed)
                if entry.collected.get(cid, _ZERO) < executed:
                    await self._collect_fills(entry, raw, tracked.kind, client_id=cid)
                    entry.collected[cid] = executed
            if str(raw["status"]) in _DONE:
                entry.final.add(cid)

    @staticmethod
    def _track_legs(entry: _Entry, generation: int, ids: dict[str, str], *, oco: bool) -> None:
        if oco:
            entry.tracked[ids["below"]] = _Tracked(FillKind.STOP, generation)
            entry.tracked[ids["above"]] = _Tracked(FillKind.TARGET, generation)
        else:
            entry.tracked[derived_id(entry.entry_id, f"X{generation}")] = _Tracked(FillKind.STOP, generation)

    # ---- exposicao e reprotecao -------------------------------------------------------------------

    def _expose(self, entry: _Entry, stop: Decimal, target: Decimal | None) -> None:
        if entry.state != "exposed":
            entry.exposed_since, entry.exposure_alerted = self._mono(), False
        entry.state, entry.protection = "exposed", None
        entry.want_stop, entry.want_target = stop, target

    def _alert_exposure(self, entry: _Entry, reason: str) -> None:
        if not entry.exposure_alerted:
            entry.exposure_alerted = True
            self._alert(
                "critical",
                f"{entry.entry_id}: posicao SEM PROTECAO ({reason}); orcamento de "
                f"{self._config.protection_deadline_seconds:g}s antes da venda de emergencia",
            )

    def _budget_left(self, entry: _Entry) -> float:
        since = entry.exposed_since if entry.exposed_since is not None else self._mono()
        return max(0.0, since + self._config.protection_deadline_seconds - self._mono())

    async def _reprotect(self, entry: _Entry, *, reason: str) -> OrderReport:
        """entry.state == 'exposed': confere o ledger, adota protecao viva da ultima geracao ou recoloca;
        falhando (ou estourando o orcamento de tempo), vende o que ESTA entrada deve."""
        try:
            async with asyncio.timeout(self._budget_left(entry)):
                filters = await self._filters_or_stale(entry.symbol)
                owed = await self._fresh_owed(entry)
                if await self._adopt_live_protection(entry):
                    return await self._protection_report(entry)
                free = await self._free_base(entry.symbol)
        except TimeoutError:
            return await self._deadline_exit(entry, reason)
        quantity = filters.round_quantity_down(min(owed, free))
        if self._sold(entry) > 0 and quantity < filters.min_qty:
            return await self._close_flat(entry, "posicao encerrada pela protecao")
        if entry.want_stop is None:
            entry.want_stop = entry.order.stop_loss
            entry.want_target = entry.order.take_profit
        return await self._arm(entry, quantity, entry.want_stop, entry.want_target, filters, reason=reason)

    async def _deadline_exit(self, entry: _Entry, reason: str) -> OrderReport:
        """Orcamento de tempo da protecao esgotado: PAUSA e vai DIRETO para a venda de emergencia."""
        self._halt(
            f"{entry.entry_id}: protecao nao ficou ativa em {self._config.protection_deadline_seconds:g}s "
            f"({reason}); venda de emergencia"
        )
        return await self._emergency_exit(entry, f"{reason}; orcamento de tempo da protecao esgotado")

    async def _close_flat(self, entry: _Entry, reason: str) -> OrderReport:
        entry.emergency_alerted_at = None
        if entry.buy_open:
            entry.state, entry.protection = "working", None
            return await self._protection_report(entry)
        entry.state, entry.closed_reason, entry.protection = "closed", reason, None
        return await self._protection_report(entry)

    async def _filters_or_stale(self, symbol: Symbol) -> SymbolFilters:
        try:
            return await self._filters.get(symbol)
        except LiveError:
            if (stale := self._filters.stale(symbol)) is None:
                raise
            return stale

    async def _adopt_live_protection(self, entry: _Entry) -> bool:
        """Resultado desconhecido de um POST anterior pode ter criado a protecao: nao duplicar, adotar."""
        if entry.generation < 1 or entry.want_stop is None:
            return False
        generation = entry.generation
        if entry.want_target is not None:
            cid = self._ids(entry.entry_id, generation)["below"]
        else:
            cid = derived_id(entry.entry_id, f"X{generation}")
        leg = await self._get_order(entry.symbol, cid, critical=True)
        if leg is None or str(leg["status"]) != "NEW":
            return False
        entry.protection = _Protection(
            Decimal(leg["origQty"]) - Decimal(leg["executedQty"]),
            entry.want_stop,
            entry.want_target,
            generation,
        )
        entry.state = "protected"
        return True

    async def _advance_working(self, entry: _Entry, *, after_cancel: bool = False) -> None:
        raw = await self._get_order(entry.symbol, entry.entry_id)
        if raw is None:
            await self._expire_unsent(entry)
            return
        entry.submitted = True
        executed = Decimal(raw["executedQty"])
        status = str(raw["status"])
        if executed > 0:
            await self._collect_fills(entry, raw, FillKind.ENTRY)
            entry.bought_gross = max(entry.bought_gross, executed)
        if status == "FILLED":
            entry.filled_at = entry.filled_at or self._mono()
            await self._confirm_or_rescue(entry)
        elif status in _DONE:
            if executed > 0:
                await self._rescue(entry, reason="entrada cancelada com fill parcial")
            else:
                entry.state, entry.closed_reason = "closed", f"entrada {status.lower()}"
        elif self._approval_lapsed(entry) and not after_cancel:
            await self._cancel_expired(entry)
        elif status == "PARTIALLY_FILLED" and executed > 0:
            entry.partial_since = entry.partial_since or self._mono()
            if after_cancel or self._mono() - entry.partial_since >= self._config.partial_fill_grace_seconds:
                await self._truncate_partial(entry)

    async def _expire_unsent(self, entry: _Entry) -> None:
        """GET confirmou que a compra NAO existe. Se o POST nunca foi confirmado, so descarta a entrada apos a
        aprovacao vencer ou a janela em que a corretora ainda aceitaria o POST (recvWindow + margem, contada
        da ULTIMA tentativa): um POST em voo nao pode mais vingar. Leitura que falha (desconhecido) nunca
        chega aqui: a entrada segue `working` ate a ausencia ser confirmada."""
        if entry.submitted:
            return
        window = self._config.recv_window_ms / 1000 + self._config.unsent_entry_margin_seconds
        sent_at = entry.post_attempt_at if entry.post_attempt_at is not None else entry.placed_at
        if self._mono() - sent_at < window and not self._approval_lapsed(entry):
            return
        with contextlib.suppress(LiveError):
            await self._cancel_list(entry.symbol, derived_id(entry.entry_id, "L0"), critical=False)
        self._alert("warning", f"{entry.entry_id}: entrada nunca chegou a corretora; descartada")
        self._entries.pop(entry.entry_id, None)

    def _approval_lapsed(self, entry: _Entry) -> bool:
        return entry.expires_at is not None and self._now() >= entry.expires_at

    async def _cancel_buy(self, entry: _Entry) -> dict[str, Any] | None:
        """Cancela a lista da entrada e so devolve a ordem de compra quando um GET CONFIRMA que ela nao esta
        mais viva (CANCELED/FILLED/...). None = nao confirmado: a compra GTC pode seguir viva."""
        for attempt in range(self._config.protection_retries):
            try:
                await self._cancel_list(entry.symbol, derived_id(entry.entry_id, "L0"), critical=True)
            except BinanceApiError as error:
                if error.code not in (_UNKNOWN_ORDER, _NOT_FOUND):
                    log.warning("live_buy_cancel_rejected", entry=entry.entry_id, code=error.code)
            except LiveError as error:
                log.warning("live_buy_cancel_unknown", entry=entry.entry_id, error=type(error).__name__)
                if isinstance(error, IpBannedError):
                    return None
            try:
                raw = await self._get_order(entry.symbol, entry.entry_id, critical=True)
            except LiveError:
                raw = None
            if raw is not None and str(raw["status"]) in _DONE:
                return raw
            await self._sleep(min(0.05 * 2**attempt, 2.0))
        return None

    async def _cancel_expired(self, entry: _Entry) -> None:
        """Aprovacao vencida: a entrada LIMIT GTC nao pode preencher depois, com sinal velho e fora do gate.

        Corrida: se preencher durante o cancel, o re-exame trata o fill (protecao normal).
        """
        self._ensure_writable()
        try:
            await self._cancel_list(entry.symbol, derived_id(entry.entry_id, "L0"), critical=True)
        except BinanceApiError as error:
            if error.code not in (_UNKNOWN_ORDER, _NOT_FOUND):
                raise
        except OutcomeUnknownError:
            pass
        await self._advance_working(entry, after_cancel=True)

    async def _truncate_partial(self, entry: _Entry) -> None:
        """Fill parcial nao ativa as pernas OPOCO: cancela o resto (CONFIRMADO por GET) e protege o executado.

        Sem confirmacao a compra GTC pode seguir viva: o executado recebe IMEDIATAMENTE um stop avulso (OCO),
        a entrada fica `protected` com `buy_open`, o gateway pausa e alerta; cada ciclo repete o cancel e
        cobre o delta de um fill posterior (`_settle_open_buy`)."""
        self._ensure_writable()
        refreshed = await self._cancel_buy(entry)
        if refreshed is None:
            self._halt(
                f"{entry.entry_id}: compra parcial com cancelamento NAO confirmado (ordem pode seguir viva); "
                "executado protegido por stop avulso, fill posterior sera coberto no proximo ciclo"
            )
            entry.buy_open = True
            self._expose(entry, entry.order.stop_loss, entry.order.take_profit)
            self._alert_exposure(entry, "compra parcial com cancelamento nao confirmado")
            await self._reprotect(entry, reason="fill parcial com cancelamento nao confirmado")
            raise ProtectionError(f"{entry.entry_id}: cancelamento da compra parcial nao confirmado")
        entry.buy_open = False
        executed = Decimal(refreshed["executedQty"])
        if executed > 0:
            await self._collect_fills(entry, refreshed, FillKind.ENTRY)
            entry.bought_gross = max(entry.bought_gross, executed)
        await self._rescue(entry, reason="fill parcial sem protecao")

    async def _confirm_or_rescue(self, entry: _Entry) -> None:
        ids = self._ids(entry.entry_id, 0)
        stop_leg = await self._get_order(entry.symbol, ids["below"])
        status = str(stop_leg["status"]) if stop_leg else "MISSING"
        if status == "NEW":
            self._adopt_opoco_legs(entry, stop_leg)
            return
        sold_before = self._sold(entry)
        await self._sync_ledger(entry)
        if self._sold(entry) > sold_before:
            filters = await self._filters_or_stale(entry.symbol)
            if self._is_flat(entry, filters):
                await self._close_flat(entry, "alvo/stop executou antes do primeiro ciclo")
                return
            self._expose(entry, entry.order.stop_loss, entry.order.take_profit)
            self._alert_exposure(entry, "stop/alvo executou antes do primeiro ciclo")
            await self._reprotect(entry, reason="protecao encerrada antes do primeiro ciclo")
            return
        waited = self._mono() - (entry.filled_at or self._mono())
        if status == "PENDING_NEW" and waited < self._config.protection_activation_grace_seconds:
            return
        await self._rescue(entry, reason=f"OCO nao ativada apos o fill (perna stop: {status})")

    async def _rescue(self, entry: _Entry, *, reason: str) -> None:
        """Posicao sem protecao: limpa pernas mortas, coloca OCO avulsa; falhando, vende a mercado."""
        self._ensure_writable()
        self._expose(entry, entry.order.stop_loss, entry.order.take_profit)
        self._alert_exposure(entry, reason)
        with contextlib.suppress(BinanceApiError, OutcomeUnknownError):
            await self._cancel_list(entry.symbol, derived_id(entry.entry_id, "L0"), critical=True)
        await self._reprotect(entry, reason=reason)

    async def _arm(
        self,
        entry: _Entry,
        quantity: Decimal,
        stop: Decimal,
        target: Decimal | None,
        filters: SymbolFilters,
        *,
        reason: str,
        routine: bool = False,
    ) -> OrderReport:
        """Protege `quantity` (OCO ou stop puro) dentro do ORCAMENTO DE TEMPO da exposicao. NUNCA deixa
        excecao vazar com a posicao exposta: qualquer falha ou estouro do orcamento => venda de emergencia
        => (falhando) PAUSA + alerta critico, estado `exposed`. `routine` = janela deliberada (raise_stop)."""
        self._expose(entry, stop, target)
        if not routine:
            self._alert_exposure(entry, reason)
        try:
            async with asyncio.timeout(self._budget_left(entry)):
                return await self._arm_attempts(entry, quantity, stop, target, filters, reason=reason)
        except ProtectionError:
            raise
        except asyncio.CancelledError:
            self._halt(f"{entry.entry_id}: cancelado com a posicao SEM PROTECAO")
            raise
        except TimeoutError:
            return await self._deadline_exit(entry, reason)
        except Exception as error:
            log.error("live_arm_failed", entry=entry.entry_id, error=type(error).__name__)
            return await self._emergency_exit(entry, f"{reason}; {type(error).__name__}")

    async def _arm_attempts(
        self,
        entry: _Entry,
        quantity: Decimal,
        stop: Decimal,
        target: Decimal | None,
        filters: SymbolFilters,
        *,
        reason: str,
    ) -> OrderReport:
        entry.generation += 1
        generation = entry.generation
        ids = self._ids(entry.entry_id, generation)
        self._track_legs(entry, generation, ids, oco=target is not None)
        if quantity < filters.min_qty or quantity * stop < filters.min_notional:
            self._alert(
                "critical", f"{entry.entry_id}: quantidade {quantity} abaixo do minimo, sem protecao possivel"
            )
            return await self._close_flat(entry, "poeira sem tamanho minimo")
        last_error: Exception | None = None
        for attempt in range(self._config.protection_retries):
            try:
                if target is not None:
                    await self._post_oco(entry, quantity, stop, target, ids, filters)
                else:
                    await self._post_stop(entry, quantity, stop, generation, filters)
            except (OutcomeUnknownError, OrderOutcomeUnknownError) as error:
                last_error = error
                if await self._protection_present(entry, ids, target is not None, generation):
                    break
            except ClockDriftError as error:
                last_error = error
            except BinanceApiError as error:
                last_error = error
                if error.code == _REJECTED and "duplicate" in error.message.lower():
                    break
                if error.code == _WOULD_TRIGGER:
                    return await self._emergency_exit(entry, f"{reason}; stop dispararia na hora")
                if error.code == _REJECTED:
                    free = await self._free_base(entry.symbol)
                    quantity = filters.round_quantity_down(min(quantity, free, await self._fresh_owed(entry)))
                    if quantity < filters.min_qty:
                        return await self._close_flat(entry, "sem saldo para proteger")
            else:
                break
            await self._sleep(0.05 * (attempt + 1))
        else:
            log.error("live_protection_failed", entry=entry.entry_id, error=type(last_error).__name__)
            return await self._emergency_exit(entry, f"{reason}; protecao falhou apos retries")
        entry.protection = _Protection(quantity, stop, target, generation)
        entry.state, entry.emergency_alerted_at = "protected", None
        return await self._protection_report(entry)

    async def _post_oco(
        self,
        entry: _Entry,
        quantity: Decimal,
        stop: Decimal,
        target: Decimal,
        ids: dict[str, str],
        filters: SymbolFilters,
    ) -> None:
        params = {
            "symbol": to_exchange_symbol(entry.symbol),
            "side": "SELL",
            "quantity": _fmt(quantity),
            "listClientOrderId": ids["list"],
            "aboveType": "LIMIT_MAKER",
            "aboveClientOrderId": ids["above"],
            "abovePrice": _fmt(target),
            "belowClientOrderId": ids["below"],
        }
        params |= self._stop_params("below", stop, filters)
        await self._post_idempotent(
            "/api/v3/orderList/oco",
            params,
            lookup=lambda: self._get_list(entry.symbol, ids["list"], critical=True),
            critical=True,
        )

    async def _post_stop(
        self, entry: _Entry, quantity: Decimal, stop: Decimal, generation: int, filters: SymbolFilters
    ) -> None:
        cid = derived_id(entry.entry_id, f"X{generation}")
        params = {
            "symbol": to_exchange_symbol(entry.symbol),
            "side": "SELL",
            "quantity": _fmt(quantity),
            "newClientOrderId": cid,
            "newOrderRespType": "ACK",
        }
        leg = self._stop_params("", stop, filters)
        params |= {"type": leg["Type"], "stopPrice": leg["StopPrice"]}
        if "Price" in leg:
            params |= {"price": leg["Price"], "timeInForce": leg["TimeInForce"]}
        await self._post_idempotent(
            "/api/v3/order",
            params,
            lookup=lambda: self._get_order(entry.symbol, cid, critical=True),
            critical=True,
        )

    async def _protection_present(
        self, entry: _Entry, ids: dict[str, str], is_oco: bool, generation: int
    ) -> bool:
        try:
            if is_oco:
                return await self._get_list(entry.symbol, ids["list"], critical=True) is not None
            return (
                await self._get_order(
                    entry.symbol, derived_id(entry.entry_id, f"X{generation}"), critical=True
                )
                is not None
            )
        except LiveError:
            return False

    async def _emergency_exit(self, entry: _Entry, reason: str) -> OrderReport:
        """Venda a mercado SOMENTE do que o ledger da entrada deve (owed), nunca o saldo total.

        Confere executedQty; sobra => repete (backoff, orcamento de falhas). Esgotado, ou IP banido: a posicao
        segue `exposed`, o gateway PAUSA + alerta critico e a retomada segura (poll) tenta de novo.
        CONSERVADOR: ledger persistentemente ilegivel IMPEDE a venda (nao ha como saber o que e da entrada e
        o que e saldo alheio): a posicao fica exposta, pausada e alertada ate a leitura voltar ou o operador
        agir (`close_orphan`).
        """
        self._alert_emergency(entry, reason)
        entry.state, entry.protection = "exposed", None
        last: Exception | None = None
        failures = 0
        checked = False
        for _ in range(200):
            if failures >= self._config.emergency_max_attempts:
                break
            try:
                filters = await self._filters_or_stale(entry.symbol)
                remaining = await self._fresh_owed(entry)
                if not checked:
                    # Um POST de protecao cancelado/sem resposta pode ter vingado: adotar em vez de vender.
                    if await self._adopt_live_protection(entry):
                        return await self._protection_report(entry)
                    checked = True
                if remaining < filters.market_min:
                    return await self._close_after_emergency(entry, reason, remaining)
                free = await self._free_base(entry.symbol)
                quantity = filters.market_chunk(min(remaining, free))
                if quantity < filters.market_min:
                    if await self._adopt_live_protection(entry):
                        return await self._protection_report(entry)
                    self._alert(
                        "critical",
                        f"{entry.entry_id}: sem saldo livre vendavel ({free}) para {remaining} devidos",
                    )
                    return await self._close_after_emergency(entry, reason, _ZERO)
                executed = await self._sell_market(entry, quantity)
                if executed < quantity:
                    failures += 1
                    last = LiveError(f"venda de emergencia executou {executed} de {quantity}")
                    await self._sleep(min(0.05 * 2**failures, 2.0))
            except IpBannedError as error:
                last = error
                break
            except asyncio.CancelledError:
                self._halt(f"{entry.entry_id}: cancelado com a posicao SEM PROTECAO")
                raise
            except Exception as error:
                failures += 1
                last = error
                if isinstance(error, BinanceApiError):
                    if error.status == 418:
                        break
                    if error.code == _FILTER_FAILURE:
                        with contextlib.suppress(LiveError):
                            await self._filters.refresh()
                await self._sleep(min(0.05 * 2**failures, 2.0))
        self._halt(
            f"{entry.entry_id}: posicao SEM PROTECAO e venda de emergencia falhou "
            f"({type(last).__name__ if last else 'sobra'}); retomada segura pendente"
        )
        raise ProtectionError(f"{entry.entry_id}: emergencia falhou; gateway pausado")

    def _alert_emergency(self, entry: _Entry, reason: str) -> None:
        """Debounce: 1 alerta critico por episodio; reescalona a cada `emergency_realert_seconds`."""
        now = self._mono()
        last = entry.emergency_alerted_at
        if last is None or now - last >= self._config.emergency_realert_seconds:
            entry.emergency_alerted_at = now
            self._alert("critical", f"{entry.entry_id}: venda de emergencia ({reason})")

    async def _close_after_emergency(self, entry: _Entry, reason: str, dust: Decimal) -> OrderReport:
        if dust > 0:
            self._alert(
                "critical", f"{entry.entry_id}: sobra {dust} abaixo do minimo de venda (poeira) sem protecao"
            )
        return await self._close_flat(entry, f"emergencia: {reason}")

    async def _sell_market(self, entry: _Entry, quantity: Decimal) -> Decimal:
        """Ordem a mercado de emergencia (id deterministico por tentativa); devolve a quantidade executada."""
        cid = derived_id(entry.entry_id, f"EM{entry.em_seq}")
        entry.tracked[cid] = _Tracked(FillKind.EMERGENCY, None)
        params = {
            "symbol": to_exchange_symbol(entry.symbol),
            "side": "SELL",
            "type": "MARKET",
            "quantity": _fmt(quantity),
            "newClientOrderId": cid,
            "newOrderRespType": "RESULT",
        }
        await self._post_idempotent(
            "/api/v3/order",
            params,
            lookup=lambda: self._get_order(entry.symbol, cid, critical=True),
            critical=True,
        )
        raw = await self._get_order(entry.symbol, cid, critical=True)
        if raw is None or str(raw["status"]) not in _DONE:
            raise OrderOutcomeUnknownError(f"{cid}: venda de emergencia nao confirmada")
        entry.em_seq += 1
        executed = Decimal(raw["executedQty"])
        entry.final.add(cid)
        if executed > 0:
            self._record_sale(entry, cid, executed)
            try:
                await self._collect_fills(entry, raw, FillKind.EMERGENCY)
                entry.collected[cid] = executed
            except LiveError as error:
                log.warning("live_fill_collect_failed", entry=entry.entry_id, error=type(error).__name__)
        return executed

    async def _settle_open_buy(self, entry: _Entry) -> None:
        """Compra parcial com cancelamento antes nao confirmado: repete o cancel, contabiliza o executado e
        cobre o delta (fill posterior) com nova protecao sobre o `owed` total."""
        raw = await self._get_order(entry.symbol, entry.entry_id)
        if raw is None:
            return
        if str(raw["status"]) not in _DONE:
            raw = await self._cancel_buy(entry) or raw
        if (executed := Decimal(raw["executedQty"])) > entry.bought_gross:
            await self._collect_fills(entry, raw, FillKind.ENTRY)
            entry.bought_gross = executed
        if str(raw["status"]) in _DONE:
            entry.buy_open = False
        prot = entry.protection
        if entry.state != "protected" or prot is None:
            return
        filters = await self._filters_or_stale(entry.symbol)
        total = filters.round_quantity_down(await self._fresh_owed(entry))
        if total - prot.quantity >= filters.min_qty and total * prot.stop >= filters.min_notional:
            await self._replace(entry, prot, prot.stop, prot.target, filters)

    async def _advance_protected(self, entry: _Entry) -> None:
        if entry.buy_open:
            await self._settle_open_buy(entry)
            if entry.state != "protected":
                return
        prot = entry.protection
        if prot is None:
            self._expose(entry, entry.want_stop or entry.order.stop_loss, entry.want_target)
            await self._reprotect(entry, reason="protecao ausente")
            return
        ids = self._ids(entry.entry_id, prot.generation)
        if prot.kind == "oco":
            listing = await self._get_list(entry.symbol, ids["list"])
            done = listing is not None and listing.get("listOrderStatus") == "ALL_DONE"
            missing = listing is None
        else:
            leg = await self._get_order(entry.symbol, derived_id(entry.entry_id, f"X{prot.generation}"))
            done = leg is not None and str(leg["status"]) in _DONE
            missing = leg is None
        if not (done or missing):
            return
        sold_before = self._sold(entry)
        try:
            filters = await self._filters_or_stale(entry.symbol)
            await self._sync_ledger(entry)
        except LiveError:
            self._ledger_unknown_after_protection_end(entry, prot)
            raise
        entry.unknown_since = None
        if self._is_flat(entry, filters):
            await self._close_flat(entry, "posicao encerrada pela protecao")
            return
        self._expose(entry, prot.stop, prot.target)
        if self._sold(entry) == sold_before:
            self._alert_exposure(entry, "protecao removida/expirada fora do robo")
        await self._reprotect(entry, reason="protecao encerrada sem venda total")

    def _ledger_unknown_after_protection_end(self, entry: _Entry, prot: _Protection) -> None:
        """A protecao terminou mas o ledger nao pode ser lido: estado DESCONHECIDO, reprocessado no proximo
        ciclo sem pausa nem alerta critico (pode ser so a venda no alvo). Persistindo alem do orcamento de
        tempo da protecao, a posicao e tratada como exposta (o ciclo pausa e a retomada segura age)."""
        now = self._mono()
        entry.unknown_since = entry.unknown_since if entry.unknown_since is not None else now
        if now - entry.unknown_since >= self._config.protection_deadline_seconds:
            self._expose(entry, prot.stop, prot.target)
            self._alert_exposure(entry, "protecao encerrada e ledger ilegivel alem do prazo")

    def _is_flat(self, entry: _Entry, filters: SymbolFilters) -> bool:
        return self._sold(entry) > 0 and filters.round_quantity_down(self._owed(entry)) < filters.min_qty

    # ---- raise_stop / drop_target -----------------------------------------------------------------

    async def raise_stop(self, client_order_id: str, new_stop: Decimal) -> OrderReport:
        """Sobe o stop: cancel+replace do OCO com janela minima; falhando o replace, vende a mercado.

        Risco residual: entre o cancel e o novo OCO (2 requisicoes) a posicao fica sem stop na corretora.
        A entrada e marcada `exposed` ANTES do DELETE (cancelamento da task ou queda no meio => ja pausa e
        alerta). Nenhuma leitura ocorre na janela. Qualquer falha apos o cancel: recoloca protecao; falhando,
        vende a mercado; falhando, PAUSA + alerta critico, `exposed`. Se o preco cruzar o novo stop nessa
        janela, a OCO recusa por disparo imediato e a venda de emergencia a mercado e acionada. Se a
        corretora/IP estiver indisponivel nessa janela a venda tambem falha: o gateway pausa e alerta (nao ha
        como garantir mais que isso em spot sem ordem atomica de substituicao).
        """
        entry = self._entry(client_order_id)
        await self._repair_exposed()
        async with entry.lock, self._watch(entry):
            prot = self._active_protection(entry)
            filters = await self._filters.get(entry.symbol)
            stop = filters.round_price_up(new_stop)
            if stop <= prot.stop:
                raise ValueError("stop can only be raised")
            if prot.target is not None and stop >= prot.target:
                raise ValueError("stop must stay below target and the current bid")
            bid = await self._bid(entry.symbol)
            if stop >= bid * (1 - self._config.min_stop_distance_bps / _BPS):
                raise ValueError("stop must stay below target and the current bid")
            self._ensure_writable()
            return await self._replace(entry, prot, stop, prot.target, filters)

    async def drop_target(self, client_order_id: str) -> OrderReport:
        entry = self._entry(client_order_id)
        await self._repair_exposed()
        async with entry.lock, self._watch(entry):
            prot = self._active_protection(entry)
            if prot.target is None:
                return await self._protection_report(entry)
            self._ensure_writable()
            filters = await self._filters.get(entry.symbol)
            return await self._replace(entry, prot, prot.stop, None, filters)

    async def _replace(
        self, entry: _Entry, prot: _Protection, stop: Decimal, target: Decimal | None, filters: SymbolFilters
    ) -> OrderReport:
        self._expose(entry, prot.stop, prot.target)
        # Dentro da janela nao ha leitura extra: o DELETE devolve o executado das pernas (`_record_reports`).
        legs_sold = await self._cancel_protection(entry, prot)
        quantity = filters.round_quantity_down(self._owed(entry))
        if quantity < filters.min_qty:
            return await self._close_flat(entry, "posicao encerrada pela protecao")
        report = await self._arm(
            entry, quantity, stop, target, filters, reason="substituicao do stop falhou", routine=True
        )
        if legs_sold:
            with contextlib.suppress(LiveError):
                await self._sync_ledger(entry)
        return report

    async def _cancel_protection(self, entry: _Entry, prot: _Protection) -> bool:
        """Caminho autorizado de cancelamento (so raise_stop/drop_target/venda). O CHAMADOR ja marcou a
        entrada `exposed` (antes do DELETE). Retorna com a protecao confirmadamente encerrada (True = alguma
        perna havia executado). ProtectionError: segue ativa (estado restaurado) ou DESCONHECIDA (exposta)."""
        ids = self._ids(entry.entry_id, prot.generation)
        try:
            if prot.kind == "oco":
                response = await self._cancel_list(entry.symbol, ids["list"], critical=True)
            else:
                response = await self._client.request(
                    "DELETE",
                    "/api/v3/order",
                    {
                        "symbol": to_exchange_symbol(entry.symbol),
                        "origClientOrderId": derived_id(entry.entry_id, f"X{prot.generation}"),
                    },
                    signed=True,
                    critical=True,
                )
        except BinanceApiError as error:
            if error.code not in (_UNKNOWN_ORDER, _NOT_FOUND):
                await self._verify_cancel(entry, prot, error)
        except OutcomeUnknownError as error:
            await self._verify_cancel(entry, prot, error)
        else:
            return self._record_reports(entry, response)
        before = self._sold(entry)
        await self._sync_ledger(entry)
        return self._sold(entry) > before

    async def _verify_cancel(self, entry: _Entry, prot: _Protection, error: LiveError) -> None:
        """DELETE sem resposta valida: o estado e DESCONHECIDO ate um GET provar o contrario."""
        match await self._protection_state(entry, prot):
            case "active":
                entry.state, entry.protection = "protected", prot
                if isinstance(error, BinanceApiError):
                    raise error
                raise ProtectionError("cancelamento nao confirmado; protecao mantida") from None
            case "unknown":
                raise ProtectionError(
                    "cancelamento nao confirmado e protecao desconhecida; posicao tratada como exposta"
                ) from None

    async def _protection_state(
        self, entry: _Entry, prot: _Protection
    ) -> Literal["active", "ended", "unknown"]:
        ids = self._ids(entry.entry_id, prot.generation)
        try:
            if prot.kind == "oco":
                listing = await self._get_list(entry.symbol, ids["list"], critical=True)
                if listing is None:
                    return "unknown"
                return "active" if listing.get("listOrderStatus") == "EXECUTING" else "ended"
            raw = await self._get_order(
                entry.symbol, derived_id(entry.entry_id, f"X{prot.generation}"), critical=True
            )
            if raw is None:
                return "unknown"
            return "ended" if str(raw["status"]) in _DONE else "active"
        except LiveError:
            return "unknown"

    # ---- saidas -----------------------------------------------------------------------------------

    async def _place_exit(self, order: ExitOrder) -> OrderReport:
        """Saida so contra posicao DESTE robo: a quantidade nunca excede o `owed` de uma entrada aberta."""
        self._ensure_writable()
        await self._repair_exposed()
        filters = await self._filters.get(order.symbol)
        avg = await self._avg_price(order.symbol)
        filters.require_valid(quantity=order.quantity, price=order.price, side="SELL", avg_price=avg)
        is_market = order.type is not OrderType.LIMIT or order.price is None
        if is_market and (problems := filters.market_problems(order.quantity)):
            raise FilterViolationError("; ".join(problems))
        cid = order.client_order_id
        params = {
            "symbol": to_exchange_symbol(order.symbol),
            "side": "SELL",
            "quantity": _fmt(order.quantity),
            "newClientOrderId": cid,
            "newOrderRespType": "RESULT",
        }
        if order.type is OrderType.LIMIT and order.price is not None:
            params |= {"type": "LIMIT", "price": _fmt(order.price), "timeInForce": "IOC"}
        else:
            params["type"] = "MARKET"
        open_entries = [e for e in self._entries.values() if e.symbol == order.symbol and e.state != "closed"]
        if not open_entries:
            raise FilterViolationError("saida sem posicao aberta deste robo: saldo alheio nunca e vendido")
        async with contextlib.AsyncExitStack() as stack:
            for held in open_entries:
                await stack.enter_async_context(held.lock)
            candidates = sorted(open_entries, key=lambda e: e.state != "protected")
            entry = None
            for held in candidates:
                if held.state != "protected":
                    await self._settle_filled_buy(held)
                if held.state != "closed" and order.quantity <= await self._fresh_owed(held):
                    entry = held
                    break
            if entry is None:
                raise FilterViolationError("saida maior que a posicao das entradas abertas")
            async with self._watch(entry):
                if entry.state == "protected":
                    await self._exit_protected(entry, order, params, filters)
                else:
                    await self._exit_unprotected(entry, order, params, filters)
        return await self._exit_report(order)

    async def _settle_filled_buy(self, entry: _Entry) -> None:
        """Poe a entrada em dia ANTES de calcular o owed de uma saida: contabiliza o fill e, se a compra ja
        encheu, uma OCO viva so na corretora vira `protected` (a saida passa pelo caminho com cancel, sem a
        corrida alvo/stop x venda); pernas ainda `PENDING_NEW` sao canceladas antes de vender."""
        if entry.state != "working":
            return
        raw = await self._get_order(entry.symbol, entry.entry_id, critical=True)
        if raw is None:
            return
        if (executed := Decimal(raw["executedQty"])) > 0:
            await self._collect_fills(entry, raw, FillKind.ENTRY)
            entry.bought_gross = max(entry.bought_gross, executed)
        if str(raw["status"]) != "FILLED":
            return
        entry.filled_at = entry.filled_at or self._mono()
        leg = await self._get_order(entry.symbol, self._ids(entry.entry_id, 0)["below"], critical=True)
        match str(leg["status"]) if leg else "MISSING":
            case "NEW":
                self._adopt_opoco_legs(entry, leg)
            case "PENDING_NEW":
                with contextlib.suppress(BinanceApiError):
                    await self._cancel_list(entry.symbol, derived_id(entry.entry_id, "L0"), critical=True)

    def _adopt_opoco_legs(self, entry: _Entry, stop_leg: dict[str, Any] | None) -> None:
        o = entry.order
        quantity = Decimal(stop_leg["origQty"]) if stop_leg else o.quantity
        entry.protection = _Protection(quantity, o.stop_loss, o.take_profit, 0)
        entry.state = "protected"

    async def _post_exit(
        self, entry: _Entry, order: ExitOrder, params: dict[str, str]
    ) -> tuple[bool, Exception | None]:
        """Envia a saida e registra o executado no ledger da entrada. (conhecido, erro): conhecido=False =
        nao foi possivel confirmar o que a corretora fez com a ordem."""
        cid = order.client_order_id
        entry.tracked[cid] = _Tracked(FillKind.EXIT, None)
        error: Exception | None = None
        try:
            await self._post_idempotent(
                "/api/v3/order",
                params,
                lookup=lambda: self._get_order(order.symbol, cid, critical=True),
                critical=True,
            )
            self._submitted.append(order)
        except (LiveError, ValueError) as caught:
            error = caught
        try:
            raw = await self._get_order(order.symbol, cid, critical=True)
        except LiveError as caught:
            log.warning("live_exit_read_failed", entry=entry.entry_id, error=type(caught).__name__)
            return False, error
        if raw is not None and (executed := Decimal(raw["executedQty"])) > 0:
            self._record_sale(entry, cid, executed)
            try:
                await self._collect_fills(entry, raw, FillKind.EXIT)
                entry.collected[cid] = executed
            except LiveError as caught:
                log.warning("live_fill_collect_failed", entry=entry.entry_id, error=type(caught).__name__)
        if raw is not None and str(raw["status"]) in _DONE:
            entry.final.add(cid)
        return True, error

    async def _clamp_exit(
        self, entry: _Entry, order: ExitOrder, params: dict[str, str], filters: SymbolFilters
    ) -> dict[str, str]:
        """Quantidade da saida = min(pedido, owed recente, saldo livre); abaixo do lote minimo, recusa."""
        free = await self._free_base(entry.symbol)
        quantity = filters.round_quantity_down(min(order.quantity, self._owed(entry), free))
        if quantity < filters.min_qty:
            raise FilterViolationError("saldo livre/posicao da entrada abaixo do lote minimo de saida")
        return params | {"quantity": _fmt(quantity)}

    async def _exit_unprotected(
        self, entry: _Entry, order: ExitOrder, params: dict[str, str], filters: SymbolFilters
    ) -> None:
        params = await self._clamp_exit(entry, order, params, filters)
        _, error = await self._post_exit(entry, order, params)
        if error is not None:
            raise error

    async def _exit_protected(
        self, entry: _Entry, order: ExitOrder, params: dict[str, str], filters: SymbolFilters
    ) -> None:
        prot = self._active_protection(entry)
        self._expose(entry, prot.stop, prot.target)
        sold_before = self._sold(entry)
        legs_sold = await self._cancel_protection(entry, prot)
        if self._sold(entry) > sold_before:
            await self._reprotect(entry, reason="protecao encerrada durante a saida")
            raise ProtectionError("protecao encerrada durante a saida; reconsulte a posicao")
        # `owed` acabou de ser conferido (cancel + sync): ja limita a venda ao que a entrada detem.
        with contextlib.suppress(LiveError):
            params = await self._clamp_exit(entry, order, params, filters)
        known, error = await self._post_exit(entry, order, params)
        if not known:
            self._halt(
                f"{entry.entry_id}: saida {order.client_order_id} sem confirmacao e posicao SEM PROTECAO; "
                "a retomada segura confere o ledger antes de reproteger"
            )
            raise error or ProtectionError(f"{entry.entry_id}: resultado da saida desconhecido")
        remaining = filters.round_quantity_down(self._owed(entry))
        if remaining >= filters.min_qty and remaining * prot.stop >= filters.min_notional:
            await self._arm(
                entry,
                remaining,
                prot.stop,
                prot.target,
                filters,
                reason="reproteger apos saida",
                routine=True,
            )
        else:
            await self._close_flat(entry, "saida executada")
        if legs_sold:
            with contextlib.suppress(LiveError):
                await self._sync_ledger(entry)
        if error is not None:
            raise error

    async def _exit_report(self, order: ExitOrder) -> OrderReport:
        raw = await self._get_order(order.symbol, order.client_order_id)
        if raw is None:
            raise UnknownOrderError(order.client_order_id)
        return self._report_from(raw, order.symbol)

    # ---- fills ------------------------------------------------------------------------------------

    async def _collect_fills(
        self, entry: _Entry, raw: dict[str, Any], kind: FillKind, *, client_id: str | None = None
    ) -> None:
        trades = await self._client.request(
            "GET",
            "/api/v3/myTrades",
            {"symbol": to_exchange_symbol(entry.symbol), "orderId": raw["orderId"]},
            signed=True,
            weight=20,
            critical=True,
        )
        base, quote = entry.symbol.split("/")
        rates: dict[str, Decimal | None] = {}
        for trade in trades:
            asset, commission = str(trade["commissionAsset"]), Decimal(trade["commission"])
            cost: Decimal | None = None
            estimated = False
            if (
                asset not in (base, quote)
                and commission > 0
                and not self._fills.knows(entry.symbol, int(trade["id"]))
            ):
                if asset not in rates:
                    rates[asset] = await self._asset_rate(asset)
                rate = rates[asset]
                if rate is not None:
                    cost = commission * rate
                else:
                    cost = Decimal(trade["quoteQty"]) * self._config.fallback_fee_rate
                    estimated = True
            self._fills.record(
                trade,
                symbol=entry.symbol,
                entry_id=entry.entry_id,
                order_client_id=client_id or str(raw["clientOrderId"]),
                kind=kind,
                commission_quote=cost,
                commission_estimated=estimated,
            )

    async def _asset_rate(self, asset: str) -> Decimal | None:
        """Cotacao do momento (asset em quote) p/ converter comissao em BNB etc.; None = indisponivel."""
        try:
            data = await self._client.request(
                "GET",
                "/api/v3/ticker/price",
                {"symbol": f"{asset}{self._config.quote_asset}"},
                weight=2,
                critical=True,
            )
            rate = Decimal(data["price"])
        except (LiveError, KeyError, ArithmeticError):
            return None
        return rate if rate > 0 else None

    # ---- relatorios -------------------------------------------------------------------------------

    def _report_from(self, raw: dict[str, Any], symbol: Symbol) -> OrderReport:
        executed = Decimal(raw["executedQty"])
        quote = Decimal(raw.get("cummulativeQuoteQty", "0"))
        return OrderReport(
            order_id=str(raw["orderId"]),
            client_order_id=str(raw["clientOrderId"]),
            symbol=symbol,
            status=_order_status(raw),
            filled_quantity=executed,
            average_price=(quote / executed) if executed > 0 else None,
        )

    async def _entry_report(self, entry: _Entry) -> OrderReport:
        raw = await self._get_order(entry.symbol, entry.entry_id)
        if raw is None:
            return OrderReport(
                order_id="pending",
                client_order_id=entry.entry_id,
                symbol=entry.symbol,
                status=OrderStatus.NEW,
                filled_quantity=_ZERO,
            )
        return self._report_from(raw, entry.symbol)

    async def _protection_report(self, entry: _Entry) -> OrderReport:
        prot = entry.protection
        active = entry.state == "protected" and prot is not None
        return OrderReport(
            order_id=derived_id(entry.entry_id, f"L{prot.generation if prot else entry.generation}"),
            client_order_id=f"{entry.entry_id}{OCO_SUFFIX}",
            symbol=entry.symbol,
            status=OrderStatus.NEW
            if active
            else OrderStatus.FILLED
            if entry.state == "closed"
            else OrderStatus.CANCELED,
            filled_quantity=_ZERO,
        )

    def _entry(self, entry_id: str) -> _Entry:
        try:
            return self._entries[entry_id]
        except KeyError:
            raise UnknownOrderError(entry_id) from None

    def _active_protection(self, entry: _Entry) -> _Protection:
        if entry.state != "protected" or entry.protection is None:
            raise UnknownOrderError(entry.entry_id)
        return entry.protection

    # ---- recuperacao apos reinicio ----------------------------------------------------------------

    async def recover(self, entries: Sequence[PersistedEntry]) -> RecoveryResult:
        """Reconstroi, SEM estado extra, entradas de uma instancia anterior a partir dos ids deterministicos.

        Fase 1 (so leitura, todas as entradas): compra (L0), geracoes S/T/X, emergencias EM e saidas
        persistidas -> `bought_gross` e `sold` via ordem + `/myTrades` por orderId. Fase 2: vendas na conta
        que nenhuma entrada reconstruida explica (saida sem id persistido, venda manual) => entradas do
        simbolo ficam `exposed` com PAUSA + alerta, sem vender nem proteger com ledger duvidoso. Fase 3: ADOTA
        a protecao viva (reprotegendo o delta) ou reprotege. O que nao puder ser reconstruido fica `exposed`
        (pausa + alerta critico) e e retomado no proximo `poll`. Fills ja entregues numa execucao anterior
        sao reemitidos para `on_fill` (o sink deve ser idempotente por (simbolo, trade_id)).
        """
        self._ensure_writable()
        results: dict[str, RecoveredEntry] = {}
        fresh: list[_Entry] = []
        async with contextlib.AsyncExitStack() as stack:
            for persisted in entries:
                cid = persisted.order.client_order_id
                if cid in self._entries:
                    results.setdefault(
                        cid, RecoveredEntry(cid, "already_known", "entrada ja registrada neste gateway")
                    )
                    continue
                self._filters.check_allowed(persisted.order.symbol)
                entry = self._register_recovering(persisted)
                await stack.enter_async_context(entry.lock)
                await stack.enter_async_context(self._watch(entry))
                fresh.append(entry)
            reads = await self._recover_reads(fresh, results)
            for entry in fresh:
                if entry.entry_id in results:
                    continue
                try:
                    outcome, detail = await self._apply_rebuilt(entry, reads[entry.entry_id])
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    results[entry.entry_id] = self._recovery_failed(entry, error)
                else:
                    results[entry.entry_id] = RecoveredEntry(entry.entry_id, outcome, detail)
        return RecoveryResult(tuple(results[p.order.client_order_id] for p in entries))

    def _register_recovering(self, persisted: PersistedEntry) -> _Entry:
        order = persisted.order
        entry = _Entry(
            order=order,
            expires_at=persisted.expires_at,
            placed_at=self._mono(),
            rebuilt=False,
            recover=persisted,
            submitted=True,
        )
        self._entries[order.client_order_id] = entry
        self._expose(entry, order.stop_loss, order.take_profit)
        return entry

    async def _recover_reads(
        self, fresh: list[_Entry], results: dict[str, RecoveredEntry]
    ) -> dict[str, dict[str, Any]]:
        reads: dict[str, dict[str, Any]] = {}
        for entry in fresh:
            try:
                buy = await self._read_ledger(entry)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                results[entry.entry_id] = self._recovery_failed(entry, error)
                continue
            if buy is None:
                results[entry.entry_id] = RecoveredEntry(
                    entry.entry_id, "not_placed", "a compra nunca chegou a corretora"
                )
            else:
                reads[entry.entry_id] = buy
        for symbol in {e.symbol for e in fresh if e.entry_id in reads}:
            group = [e for e in fresh if e.symbol == symbol and e.entry_id in reads]
            try:
                await self._check_unattributed_sells(group)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                for entry in group:
                    reads.pop(entry.entry_id, None)
                    results[entry.entry_id] = self._recovery_failed(entry, error)
        return reads

    def _recovery_failed(self, entry: _Entry, error: Exception) -> RecoveredEntry:
        detail = f"irreconstruivel agora ({type(error).__name__}: {error}); posicao tratada como exposta"
        self._halt(f"{entry.entry_id}: recuperacao incompleta: {detail}")
        self._alert_exposure(entry, "recuperacao apos reinicio incompleta")
        return RecoveredEntry(entry.entry_id, "exposed", detail)

    async def _rebuild(self, entry: _Entry) -> tuple[RecoveryOutcome, str]:
        """Reconstrucao de UMA entrada (retomada de `exposed` nao reconstruido; `close_orphan`)."""
        buy = await self._read_ledger(entry)
        if buy is None:
            return "not_placed", "a compra nunca chegou a corretora"
        await self._check_unattributed_sells([entry])
        return await self._apply_rebuilt(entry, buy)

    async def _read_ledger(self, entry: _Entry) -> dict[str, Any] | None:
        """So leitura (+ ledger e fills): a compra, ou None se nunca chegou a corretora (descarta)."""
        symbol, cid = entry.symbol, entry.entry_id
        buy = await self._get_order(symbol, cid, critical=True)
        if buy is None:
            if await self._get_list(symbol, derived_id(cid, "L0"), critical=True) is not None:
                raise ProtectionError("lista L0 existe sem a ordem de compra")
            self._entries.pop(cid, None)
            entry.state, entry.closed_reason, entry.protection = "closed", "compra nunca enviada", None
            return None
        if (executed := Decimal(buy["executedQty"])) > 0:
            await self._collect_fills(entry, buy, FillKind.ENTRY)
            entry.bought_gross = max(entry.bought_gross, executed)
        await self._register_recovered_orders(entry)
        await self._sync_ledger(entry)
        return buy

    async def _check_unattributed_sells(self, group: list[_Entry]) -> None:
        """Venda na conta apos a compra que nenhuma ordem reconstruida explica: `owed` pode estar inflado e
        proteger/vender saldo alheio. Conservador: recusa (entrada `exposed`; manual via close_orphan)."""
        symbol = group[0].symbol
        starts = [
            int(fill.filled_at.timestamp() * 1000)
            for e in group
            for fill in self._fills.all()
            if fill.entry_id == e.entry_id and fill.kind is FillKind.ENTRY
        ]
        if not starts:
            return
        trades = await self._client.request(
            "GET",
            "/api/v3/myTrades",
            {"symbol": to_exchange_symbol(symbol), "startTime": min(starts)},
            signed=True,
            weight=20,
            critical=True,
        )
        unknown = [
            t["id"] for t in trades if not t["isBuyer"] and not self._fills.knows(symbol, int(t["id"]))
        ]
        if unknown:
            raise UnattributedSalesError(
                f"{len(unknown)} venda(s) na conta apos a compra sem ordem conhecida (trades {unknown[:5]}); "
                "informe exit_client_ids ou use close_orphan"
            )

    async def _apply_rebuilt(self, entry: _Entry, buy: dict[str, Any]) -> tuple[RecoveryOutcome, str]:
        symbol = entry.symbol
        executed = Decimal(buy["executedQty"])
        filters = await self._filters_or_stale(symbol)
        live = await self._find_live_protection(entry)
        entry.rebuilt = True
        o = entry.order
        levels = (live.stop, live.target) if live else (o.stop_loss, o.take_profit)
        entry.want_stop, entry.want_target = levels
        buy_live = str(buy["status"]) not in _DONE
        if executed == 0:
            if buy_live:
                entry.state, entry.protection = "working", None
                return "working", "compra aguardando fill"
            await self._close_flat(entry, f"entrada {str(buy['status']).lower()} sem fill")
            return "flat", "compra encerrada sem fill"
        if live is not None:
            return await self._adopt_recovered(entry, live, filters, buy_open=buy_live)
        if buy_live:
            entry.state, entry.protection = "working", None
            return "working", "fill parcial sem protecao viva; o proximo poll protege"
        if self._is_flat(entry, filters):
            await self._close_flat(entry, "posicao ja vendida antes do reinicio")
            return "flat", "posicao encerrada"
        entry.state = "working"
        await self._advance_working(entry)
        match entry.state:
            case "protected":
                return "reprotected", "protecao recolocada"
            case "closed":
                return "flat", "posicao encerrada"
            case _:
                return "working", "aguardando ativacao da protecao"

    async def _adopt_recovered(
        self, entry: _Entry, live: _Protection, filters: SymbolFilters, *, buy_open: bool
    ) -> tuple[RecoveryOutcome, str]:
        entry.protection, entry.state, entry.buy_open = live, "protected", buy_open
        entry.exposed_since = None
        total = filters.round_quantity_down(self._owed(entry))
        if total - live.quantity >= filters.min_qty and total * live.stop >= filters.min_notional:
            await self._replace(entry, live, live.stop, live.target, filters)
            return "reprotected", f"protecao adotada cobria {live.quantity} de {total}; recolocada"
        return "protected", f"protecao viva adotada (geracao {live.generation})"

    async def _register_recovered_orders(self, entry: _Entry) -> None:
        persisted = entry.recover
        if persisted is None:
            return
        cid = entry.entry_id
        entry.generation = max(entry.generation, persisted.generation + _GENERATION_LOOKAHEAD)
        for generation in range(entry.generation + 1):
            ids = self._ids(cid, generation)
            self._track_legs(entry, generation, ids, oco=True)
            self._track_legs(entry, generation, ids, oco=False)
        sequence = 0
        while sequence < _MAX_EMERGENCY_PROBES:
            em = derived_id(cid, f"EM{sequence}")
            if await self._get_order(entry.symbol, em, critical=True) is None:
                break
            entry.tracked[em] = _Tracked(FillKind.EMERGENCY, None)
            sequence += 1
        entry.em_seq = max(entry.em_seq, sequence)
        for exit_id in persisted.exit_client_ids:
            if await self._get_order(entry.symbol, exit_id, critical=True) is not None:
                entry.tracked[exit_id] = _Tracked(FillKind.EXIT, None)

    async def _find_live_protection(self, entry: _Entry) -> _Protection | None:
        """Protecao NEW de maior geracao (OCO ou stop avulso) desta entrada na corretora."""
        symbol, cid = entry.symbol, entry.entry_id
        for generation in range(entry.generation, -1, -1):
            ids = self._ids(cid, generation)
            for stop_id, above_id in (
                (ids["below"], ids["above"]),
                (derived_id(cid, f"X{generation}"), None),
            ):
                leg = await self._get_order(symbol, stop_id, critical=True)
                if leg is None or str(leg["status"]) != "NEW":
                    continue
                target: Decimal | None = None
                if above_id is not None:
                    above = await self._get_order(symbol, above_id, critical=True)
                    if above is not None and str(above["status"]) == "NEW":
                        target = Decimal(above["price"])
                return _Protection(
                    Decimal(leg["origQty"]) - Decimal(leg["executedQty"]),
                    Decimal(leg["stopPrice"]),
                    target,
                    generation,
                )
        return None

    async def close_orphan(
        self, symbol: Symbol, quantity: Decimal, operator_confirmation: OperatorConfirmation
    ) -> OrphanCloseResult:
        """CAMINHO MANUAL: vende a mercado posicao reconstruida (`exposed`) que o robo nao conseguiu proteger.

        Exige confirmacao do operador com a quantidade EXPLICITA (igual a `quantity`), motivo e identificacao.
        A venda e limitada ao MENOR entre `quantity`, a posicao reconstruida (ledger) e o saldo livre,
        respeita MARKET_LOT_SIZE (uma ordem por entrada, no maximo `maxQty`; repita) e e AUDITADA
        (log estruturado + `on_audit`, antes e depois; auditoria que falha ANTES da venda a impede). Recusa se
        houver protecao viva nao adotada: o `recover`/`poll` adota primeiro; protecao viva remanescente tem de
        ser cancelada com seguranca antes. Permitido com o gateway pausado.
        """
        self._ensure_writable()
        confirmation = operator_confirmation
        if not confirmation.operator.strip() or not confirmation.reason.strip():
            raise FilterViolationError("close_orphan exige operador e motivo")
        if confirmation.confirmed_quantity != quantity or quantity <= 0:
            raise FilterViolationError("quantidade confirmada pelo operador difere da solicitada")
        filters = await self._filters.get(symbol)
        targets = [e for e in self._entries.values() if e.symbol == symbol and e.state == "exposed"]
        if not targets:
            raise FilterViolationError("sem posicao orfa reconstruida para o simbolo")
        async with contextlib.AsyncExitStack() as stack:
            for held in targets:
                await stack.enter_async_context(held.lock)
            return await self._close_orphan_locked(symbol, quantity, confirmation, filters, targets)

    async def _close_orphan_locked(
        self,
        symbol: Symbol,
        quantity: Decimal,
        confirmation: OperatorConfirmation,
        filters: SymbolFilters,
        targets: list[_Entry],
    ) -> OrphanCloseResult:
        owed: dict[str, Decimal] = {}
        for held in targets:
            if not held.rebuilt:
                with contextlib.suppress(UnattributedSalesError):
                    # O operador confirmou a quantidade explicitamente: o ledger ja foi lido e sincronizado.
                    await self._rebuild(held)
            if held.state != "exposed":
                continue
            if await self._find_live_protection(held) is not None:
                raise ProtectionError(
                    f"{held.entry_id}: protecao viva nao adotada; adote (recover/poll) ou cancele antes"
                )
            owed[held.entry_id] = await self._fresh_owed(held)
        position = sum(owed.values(), _ZERO)
        free = await self._free_base(symbol)
        applied = filters.market_chunk(min(quantity, position, free))
        if applied < filters.market_min:
            raise FilterViolationError(
                f"quantidade aplicavel {applied} abaixo do minimo de venda "
                f"(posicao reconstruida {position}, saldo livre {free})"
            )
        base: dict[str, Any] = {
            "symbol": symbol,
            "operator": confirmation.operator,
            "reason": confirmation.reason,
            "requested": str(quantity),
            "applied": str(applied),
            "position": str(position),
            "free": str(free),
        }
        self._audit("orphan_close_intent", **base)
        executed, order_ids, remaining = _ZERO, [], applied
        for held in targets:
            take = filters.round_market_quantity_down(min(remaining, owed.get(held.entry_id, _ZERO)))
            if take < filters.market_min:
                continue
            try:
                done = await self._sell_market(held, take)
            except LiveError as error:
                self._audit("orphan_close_failed", **base, entry=held.entry_id, error=type(error).__name__)
                raise
            order_ids.append(derived_id(held.entry_id, f"EM{held.em_seq - 1}"))
            executed, remaining = executed + done, remaining - take
            if self._is_flat(held, filters):
                await self._close_flat(held, f"orfao encerrado pelo operador {confirmation.operator}")
        self._audit("orphan_close_done", **base, executed=str(executed), orders=order_ids)
        return OrphanCloseResult(symbol, quantity, applied, executed, tuple(order_ids))

    def _audit(self, event: str, **fields: Any) -> None:
        record = {"event": event, "at": self._now().isoformat(), **fields}
        log.warning("live_audit", **{("audit" if k == "event" else k): v for k, v in record.items()})
        if self._audit_cb is not None:
            self._audit_cb(record)

    # ---- reconciliacao ----------------------------------------------------------------------------

    async def reconcile(self, local: Sequence[ManagedPosition]) -> ReconciliationResult:
        """Compara posicoes locais com /account, /openOrders e /myTrades. Divergencia => PAUSA + alerta.

        Nunca altera ordens: em particular nao 'corrige' reduzindo stop.
        """
        raw_account = await self._account()
        balances = {
            b["asset"]: Balance(asset=b["asset"], free=Decimal(b["free"]), locked=Decimal(b["locked"]))
            for b in raw_account["balances"]
            if Decimal(b["free"]) or Decimal(b["locked"])
        }
        known_cids = self._known_client_ids()
        stops: list[ProtectiveStop] = []
        stop_orders: list[dict[str, Any]] = []
        unknown: list[str] = []
        unknown_trades: list[int] = []
        for symbol in sorted(self._config.allowed_symbols):
            ex = to_exchange_symbol(symbol)
            orders = await self._client.request(
                "GET", "/api/v3/openOrders", {"symbol": ex}, signed=True, weight=6
            )
            for o in orders:
                if o["clientOrderId"] not in known_cids:
                    unknown.append(f"{symbol}:{o['clientOrderId']}")
                if o["side"] == "SELL" and o["type"] in ("STOP_LOSS", "STOP_LOSS_LIMIT"):
                    stop_orders.append(o)
                    qty = Decimal(o["origQty"]) - Decimal(o["executedQty"])
                    if qty > 0:
                        stops.append(
                            ProtectiveStop(symbol=symbol, quantity=qty, stop_price=Decimal(o["stopPrice"]))
                        )
            trades = await self._client.request(
                "GET",
                "/api/v3/myTrades",
                {"symbol": ex, "startTime": self._started_ms},
                signed=True,
                weight=20,
            )
            unknown_trades += [int(t["id"]) for t in trades if not self._fills.knows(symbol, int(t["id"]))]
        prices: dict[str, Decimal] = {}
        for asset in balances:
            symbol = f"{asset}/{self._config.quote_asset}"
            if symbol in self._config.allowed_symbols:
                prices[asset] = await self._avg_price(symbol)
        snapshot = AccountSnapshot(balances=balances, stops=tuple(stops), prices=prices)
        found = reconcile(local, snapshot, config=self._risk, ignored_assets=ignored_assets_for(self._risk))
        mismatches = self._protection_mismatches(stop_orders)
        result = ReconciliationResult(found, tuple(unknown), tuple(unknown_trades), tuple(mismatches))
        if not result.ok:
            self._halt(f"reconciliacao: {result.describe()}")
        return result

    def _known_client_ids(self) -> set[str]:
        ids: set[str] = set()
        for e in self._entries.values():
            ids.add(e.entry_id)
            for g in range(e.generation + 1):
                ids.update(self._ids(e.entry_id, g).values())
                ids.add(derived_id(e.entry_id, f"X{g}"))
            ids.update(derived_id(e.entry_id, f"EM{n}") for n in range(e.em_seq + 1))
        return ids

    def _protection_mismatches(self, stop_orders: list[dict[str, Any]]) -> list[str]:
        """Compara POR ENTRADA (id do stop vinculado + quantidade): o stop de uma nao cobre a outra."""
        by_cid = {str(o["clientOrderId"]): o for o in stop_orders}
        found: list[str] = []
        for e in self._entries.values():
            if e.state == "exposed":
                found.append(f"{e.entry_id}: posicao exposta sem protecao na corretora")
                continue
            if e.state != "protected" or e.protection is None:
                continue
            prot = e.protection
            cid = (
                self._ids(e.entry_id, prot.generation)["below"]
                if prot.kind == "oco"
                else derived_id(e.entry_id, f"X{prot.generation}")
            )
            order = by_cid.get(cid)
            if order is None:
                found.append(f"{e.entry_id}: stop {cid} ausente na corretora")
                continue
            covered = Decimal(order["origQty"]) - Decimal(order["executedQty"])
            if covered < prot.quantity:
                found.append(f"{e.entry_id}: stop na corretora cobre {covered} de {prot.quantity}")
            if Decimal(order["stopPrice"]) < prot.stop:
                found.append(f"{e.entry_id}: stop na corretora abaixo do esperado {prot.stop}")
        return found

    # ---- alertas ----------------------------------------------------------------------------------

    def _halt(self, reason: str) -> None:
        if reason != self._halt_reason:
            self._halt_reason = reason
            self._alert("critical", f"GATEWAY PAUSADO: {reason}")

    def _alert(self, level: str, message: str) -> None:
        log.error("live_alert", level=level, message=message)
        if self._alert_cb is not None:
            self._alert_cb(level, message)


def _fmt(value: Decimal) -> str:
    return format(value.normalize(), "f") if value != 0 else "0"
