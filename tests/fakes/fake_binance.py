"""Binance Spot FALSA em memoria (aiohttp) com injecao de falhas. Nunca fala com a rede real."""

import asyncio
import hashlib
import hmac
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qsl

from aiohttp import web

D = Decimal
API_KEY = "test-key-not-real"
API_SECRET = "test-secret-not-real"  # noqa: S105
_ZERO = D(0)


def _s(value: Decimal) -> str:
    return format(value, "f")


@dataclass
class Fault:
    method: str | None
    path: str
    remaining: int
    kind: str  # http | timeout | binance
    status: int = 503
    code: int = -1000
    msg: str = "fault"
    headers: dict[str, str] = field(default_factory=dict)
    after: bool = False  # processa o pedido e SO ENTAO falha (resposta perdida)


@dataclass
class FOrder:
    id: int
    cid: str
    symbol: str
    side: str
    type: str
    qty: D
    price: D = _ZERO
    stop: D = _ZERO
    tif: str = "GTC"
    status: str = "NEW"
    executed: D = _ZERO
    quote: D = _ZERO
    list_id: int = -1
    locked_quote: D = _ZERO
    locked_base: D = _ZERO


@dataclass
class FList:
    id: int
    cid: str
    symbol: str
    kind: str
    order_ids: list[int]
    status: str = "EXECUTING"


@dataclass(frozen=True)
class Req:
    method: str
    path: str
    params: dict[str, str]


class FakeBinance:
    def __init__(self, symbols: tuple[str, ...] = ("BTCUSDT",)) -> None:
        self.symbols = symbols
        self.prices: dict[str, D] = {s: D(101) for s in symbols}
        self.balances: dict[str, list[D]] = {
            "USDT": [D(10_000), _ZERO],
            "BTC": [_ZERO, _ZERO],
            "BNB": [D(5), _ZERO],
        }
        self.orders: dict[int, FOrder] = {}
        self.lists: dict[int, FList] = {}
        self.trades: list[dict[str, Any]] = []
        self.log: list[Req] = []
        self.faults: list[Fault] = []
        self.clock_offset_ms = 0
        self.commission_rate = D("0.001")
        self.commission_in_base = False
        self.fill_fraction: D | None = None
        self.hold_activation = False
        self.fail_activation = False
        self.tick, self.step, self.min_qty, self.min_notional = D("0.01"), D("0.001"), D("0.001"), D(10)
        self.market_max_qty: D | None = None
        self.market_step: D | None = None
        self.aux_prices: dict[str, D] = {"BNBUSDT": D(600)}
        self.oco_allowed = True
        self.opo_allowed = True
        self._seq = 0
        self._runner: web.AppRunner | None = None
        self.base_url = ""
        self.redirect_to: str | None = None
        self.before_dispatch: Callable[[Req], None] | None = None
        self.after_dispatch: Callable[[Req], None] | None = None
        self.exposure_windows: list[tuple[float, int]] = []
        self._exposed_since: float | None = None
        self._exposed_requests = 0

    # ---- servidor -------------------------------------------------------------------------------

    async def start(self) -> str:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._handle)
        self._runner = web.AppRunner(app, shutdown_timeout=0.1)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        server = site._server  # type: ignore[attr-defined]
        self.base_url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        return self.base_url

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    # ---- controles de teste ---------------------------------------------------------------------

    def fail(
        self,
        path: str,
        *,
        method: str | None = None,
        times: int = 1,
        kind: str = "http",
        status: int = 503,
        code: int = -1000,
        msg: str = "fault",
        headers: dict[str, str] | None = None,
        after: bool = False,
    ) -> None:
        self.faults.append(Fault(method, path, times, kind, status, code, msg, headers or {}, after))

    def reset_log(self) -> None:
        self.log.clear()

    def writes(self) -> list[Req]:
        return [r for r in self.log if r.method != "GET"]

    def order_writes(self) -> list[Req]:
        return [r for r in self.writes() if "order" in r.path.lower()]

    def free(self, asset: str) -> D:
        return self.balances.setdefault(asset, [_ZERO, _ZERO])[0]

    def locked(self, asset: str) -> D:
        return self.balances.setdefault(asset, [_ZERO, _ZERO])[1]

    def max_exposure(self) -> tuple[float, int]:
        """Maior janela com base descoberta (sem stop ativo): (segundos, requisicoes tratadas na janela)."""
        windows = self.exposure_windows or [(0.0, 0)]
        return max(w[0] for w in windows), max(w[1] for w in windows)

    def _track_exposure(self, *, request: bool = False) -> None:
        exposed = self.uncovered_base() > 0
        now = time.perf_counter()
        if exposed and self._exposed_since is None:
            self._exposed_since, self._exposed_requests = now, 0
        elif exposed and request:
            self._exposed_requests += 1
        elif not exposed and self._exposed_since is not None:
            self.exposure_windows.append((now - self._exposed_since, self._exposed_requests + request))
            self._exposed_since = None

    def active_stops(self, symbol: str = "BTCUSDT") -> list[FOrder]:
        return [
            o
            for o in self.orders.values()
            if o.symbol == symbol and o.side == "SELL" and o.stop > 0 and o.status == "NEW"
        ]

    def uncovered_base(self, asset: str = "BTC", symbol: str = "BTCUSDT") -> D:
        """Base exposta: saldo total menos o travado em stops ativos."""
        total = self.free(asset) + self.locked(asset)
        return total - sum((o.qty - o.executed for o in self.active_stops(symbol)), _ZERO)

    def set_price(self, symbol: str, price: D) -> None:
        self.prices[symbol] = price
        self._match(symbol)
        self._track_exposure()

    def complete_fill(self, cid: str) -> None:
        order = next(o for o in self.orders.values() if o.cid == cid)
        self._fill(order, order.qty - order.executed, order.price or self.prices[order.symbol], maker=True)
        self._after_fill(order)
        self._track_exposure()

    def activate_pending(self, list_cid: str) -> None:
        listing = next(item for item in self.lists.values() if item.cid == list_cid)
        self._activate(listing)
        self._track_exposure()

    # ---- roteamento -----------------------------------------------------------------------------

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        raw = (await request.text()) if request.method != "GET" else request.query_string
        params = dict(parse_qsl(raw, keep_blank_values=True))
        path = request.path
        req = Req(request.method, path, {k: v for k, v in params.items() if k != "signature"})
        self.log.append(req)
        if self.redirect_to is not None:
            raise web.HTTPFound(f"{self.redirect_to}{request.path_qs}")
        fault = self._take_fault(request.method, path)
        if fault and not fault.after:
            if fault.kind == "timeout":
                await asyncio.sleep(30)
            return self._fault_response(fault)
        if self.before_dispatch is not None:
            self.before_dispatch(req)
        status, body = self._dispatch(request, params, raw)
        if self.after_dispatch is not None:
            self.after_dispatch(req)
        self._track_exposure(request=True)
        if fault and fault.after:
            if request.transport is not None:
                request.transport.abort()
            return self._fault_response(fault)
        return web.Response(
            status=status,
            text=json.dumps(body),
            content_type="application/json",
            headers={"x-mbx-used-weight-1m": "5"},
        )

    def _take_fault(self, method: str, path: str) -> Fault | None:
        for fault in self.faults:
            if fault.remaining > 0 and fault.path == path and fault.method in (None, method):
                fault.remaining -= 1
                return fault
        return None

    def _fault_response(self, fault: Fault) -> web.Response:
        if fault.kind == "binance":
            body = {"code": fault.code, "msg": fault.msg}
            return web.Response(
                status=fault.status if fault.status != 503 else 400,
                text=json.dumps(body),
                content_type="application/json",
                headers=fault.headers,
            )
        return web.Response(status=fault.status, text=fault.msg, headers=fault.headers)

    def _dispatch(self, request: web.Request, params: dict[str, str], raw: str) -> tuple[int, Any]:
        method, path = request.method, request.path
        if path == "/api/v3/time":
            return 200, {"serverTime": int(time.time() * 1000) + self.clock_offset_ms}
        public = {
            "/api/v3/exchangeInfo": self._exchange_info,
            "/api/v3/avgPrice": self._avg_price,
            "/api/v3/ticker/price": self._ticker,
            "/api/v3/ticker/bookTicker": self._book_ticker,
        }
        if path in public and method == "GET":
            return 200, public[path](params)
        signed = {
            ("GET", "/api/v3/account"): self._account,
            ("GET", "/api/v3/order"): self._get_order,
            ("POST", "/api/v3/order"): self._post_order,
            ("DELETE", "/api/v3/order"): self._delete_order,
            ("GET", "/api/v3/orderList"): self._get_list,
            ("DELETE", "/api/v3/orderList"): self._delete_list,
            ("POST", "/api/v3/orderList/oco"): self._post_oco,
            ("POST", "/api/v3/orderList/opoco"): self._post_opoco,
            ("GET", "/api/v3/openOrders"): self._open_orders,
            ("GET", "/api/v3/myTrades"): self._my_trades,
        }
        handler = signed.get((method, path))
        if handler is None:
            return 404, {"code": -1099, "msg": "Not found"}
        if (error := self._auth(request, params, raw)) is not None:
            return error
        try:
            return 200, handler(params)
        except ApiError as error:
            return error.status, {"code": error.code, "msg": error.msg}

    def _auth(self, request: web.Request, params: dict[str, str], raw: str) -> tuple[int, Any] | None:
        if request.headers.get("X-MBX-APIKEY") != API_KEY:
            return 401, {"code": -2014, "msg": "API-key format invalid."}
        payload = raw.rsplit("&signature=", 1)[0]
        expected = hmac.new(API_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, params.get("signature", "")):
            return 400, {"code": -1022, "msg": "Signature for this request is not valid."}
        now = int(time.time() * 1000) + self.clock_offset_ms
        if abs(now - int(params.get("timestamp", 0))) > int(params.get("recvWindow", 5000)):
            return 400, {"code": -1021, "msg": "Timestamp for this request is outside of the recvWindow."}
        return None

    # ---- publicos -------------------------------------------------------------------------------

    def _exchange_info(self, params: dict[str, str]) -> dict[str, Any]:
        symbols = []
        for symbol in self.symbols:
            base = symbol.removesuffix("USDT")
            symbols.append(
                {
                    "symbol": symbol,
                    "status": "TRADING",
                    "baseAsset": base,
                    "quoteAsset": "USDT",
                    "orderTypes": ["LIMIT", "LIMIT_MAKER", "MARKET", "STOP_LOSS", "STOP_LOSS_LIMIT"],
                    "ocoAllowed": self.oco_allowed,
                    "otoAllowed": True,
                    "opoAllowed": self.opo_allowed,
                    "isSpotTradingAllowed": True,
                    "isMarginTradingAllowed": False,
                    "filters": [
                        {
                            "filterType": "PRICE_FILTER",
                            "minPrice": "0.01",
                            "maxPrice": "1000000",
                            "tickSize": _s(self.tick),
                        },
                        {
                            "filterType": "LOT_SIZE",
                            "minQty": _s(self.min_qty),
                            "maxQty": "9000",
                            "stepSize": _s(self.step),
                        },
                        {
                            "filterType": "MARKET_LOT_SIZE",
                            "minQty": _s(self.min_qty),
                            "maxQty": _s(self.market_max_qty or D(9000)),
                            "stepSize": _s(self.market_step or self.step),
                        },
                        {
                            "filterType": "NOTIONAL",
                            "minNotional": _s(self.min_notional),
                            "applyMinToMarket": True,
                            "maxNotional": "9000000",
                            "applyMaxToMarket": False,
                            "avgPriceMins": 5,
                        },
                        {
                            "filterType": "PERCENT_PRICE_BY_SIDE",
                            "bidMultiplierUp": "5",
                            "bidMultiplierDown": "0.2",
                            "askMultiplierUp": "5",
                            "askMultiplierDown": "0.2",
                            "avgPriceMins": 5,
                        },
                    ],
                }
            )
        return {"timezone": "UTC", "symbols": symbols}

    def _avg_price(self, params: dict[str, str]) -> dict[str, Any]:
        return {"mins": 5, "price": _s(self.prices[params["symbol"]])}

    def _ticker(self, params: dict[str, str]) -> dict[str, Any]:
        symbol = params["symbol"]
        return {"symbol": symbol, "price": _s(self.prices.get(symbol) or self.aux_prices[symbol])}

    def _book_ticker(self, params: dict[str, str]) -> dict[str, Any]:
        p = self.prices[params["symbol"]]
        return {
            "symbol": params["symbol"],
            "bidPrice": _s(p),
            "bidQty": "10",
            "askPrice": _s(p),
            "askQty": "10",
        }

    # ---- conta ----------------------------------------------------------------------------------

    def _account(self, params: dict[str, str]) -> dict[str, Any]:
        return {
            "permissions": ["SPOT"],
            "balances": [
                {"asset": a, "free": _s(v[0]), "locked": _s(v[1])} for a, v in self.balances.items()
            ],
        }

    def _my_trades(self, params: dict[str, str]) -> list[dict[str, Any]]:
        found = [t for t in self.trades if t["symbol"] == params["symbol"]]
        if "orderId" in params:
            found = [t for t in found if t["orderId"] == int(params["orderId"])]
        if "startTime" in params:
            found = [t for t in found if t["time"] >= int(params["startTime"])]
        return found

    def _open_orders(self, params: dict[str, str]) -> list[dict[str, Any]]:
        return [
            self._view(o) for o in self.orders.values() if o.symbol == params["symbol"] and o.status == "NEW"
        ]

    # ---- ordens ---------------------------------------------------------------------------------

    def _next_id(self) -> int:
        self._seq += 1
        return self._seq

    def _view(self, o: FOrder) -> dict[str, Any]:
        return {
            "symbol": o.symbol,
            "orderId": o.id,
            "orderListId": o.list_id,
            "clientOrderId": o.cid,
            "price": _s(o.price),
            "origQty": _s(o.qty),
            "executedQty": _s(o.executed),
            "cummulativeQuoteQty": _s(o.quote),
            "status": o.status,
            "timeInForce": o.tif,
            "type": o.type,
            "side": o.side,
            "stopPrice": _s(o.stop),
            "time": int(time.time() * 1000),
        }

    def _find(self, params: dict[str, str]) -> FOrder:
        cid = params.get("origClientOrderId")
        found = next((o for o in self.orders.values() if o.cid == cid and o.symbol == params["symbol"]), None)
        if found is None:
            raise ApiError(400, -2013, "Order does not exist.")
        return found

    def _get_order(self, params: dict[str, str]) -> dict[str, Any]:
        return self._view(self._find(params))

    def _check_cid(self, cid: str) -> None:
        if any(o.cid == cid for o in self.orders.values()):
            raise ApiError(400, -2010, "Duplicate order sent.")

    def _check_filters(self, symbol: str, qty: D, price: D | None) -> None:
        if qty < self.min_qty or qty % self.step != 0:
            raise ApiError(400, -1013, "Filter failure: LOT_SIZE")
        if price is not None and (price % self.tick != 0 or price * qty < self.min_notional):
            raise ApiError(400, -1013, "Filter failure: PRICE_FILTER/NOTIONAL")

    def _lock(self, order: FOrder) -> None:
        asset_idx = "USDT" if order.side == "BUY" else order.symbol.removesuffix("USDT")
        amount = order.price * order.qty if order.side == "BUY" else order.qty
        if self.free(asset_idx) < amount:
            raise ApiError(400, -2010, "Account has insufficient balance for requested action.")
        self.balances[asset_idx][0] -= amount
        self.balances[asset_idx][1] += amount
        if order.side == "BUY":
            order.locked_quote = amount
        else:
            order.locked_base = amount

    def _new_order(self, params: dict[str, str], *, kind: str, prefix: str = "") -> FOrder:
        symbol = params["symbol"]
        order = FOrder(
            id=self._next_id(),
            cid=params.get(f"{prefix}ClientOrderId") or params.get("newClientOrderId") or f"auto{self._seq}",
            symbol=symbol,
            side=params.get("side", "SELL"),
            type=kind,
            qty=D(params.get("quantity", "0")),
            price=D(params.get(f"{prefix}Price", params.get("price", "0"))),
            stop=D(params.get(f"{prefix}StopPrice", params.get("stopPrice", "0"))),
            tif=params.get("timeInForce", "GTC"),
        )
        return order

    def _post_order(self, params: dict[str, str]) -> dict[str, Any]:
        order = self._new_order(params, kind=params["type"])
        self._check_cid(order.cid)
        self._check_filters(order.symbol, order.qty, order.price or None)
        price = self.prices[order.symbol]
        if order.type.startswith("STOP_LOSS") and order.side == "SELL" and order.stop >= price:
            raise ApiError(400, -2021, "Order would trigger immediately.")
        if order.type == "MARKET":
            self._check_market_lot(order.qty)
            self._lock_market(order)
        else:
            self._lock(order)
        self.orders[order.id] = order
        if order.type == "MARKET":
            self._fill(order, order.qty, price, maker=False)
        elif order.type == "LIMIT":
            self._match(order.symbol)
            if order.tif == "IOC" and order.status == "NEW":
                self._release(order)
                order.status = "EXPIRED"
        return self._view(order)

    def _check_market_lot(self, qty: D) -> None:
        if (self.market_max_qty is not None and qty > self.market_max_qty) or (
            self.market_step is not None and qty % self.market_step != 0
        ):
            raise ApiError(400, -1013, "Filter failure: MARKET_LOT_SIZE")

    def _lock_market(self, order: FOrder) -> None:
        if order.side == "SELL":
            if self.free("BTC") < order.qty:
                raise ApiError(400, -2010, "Account has insufficient balance for requested action.")
            self.balances["BTC"][0] -= order.qty
            self.balances["BTC"][1] += order.qty
            order.locked_base = order.qty

    def _delete_order(self, params: dict[str, str]) -> dict[str, Any]:
        order = self._find(params)
        if order.status not in ("NEW", "PARTIALLY_FILLED"):
            raise ApiError(400, -2011, "Unknown order sent.")
        self._release(order)
        order.status = "CANCELED"
        return self._view(order)

    # ---- listas ---------------------------------------------------------------------------------

    def _list_view(self, listing: FList, *, reports: bool = False) -> dict[str, Any]:
        view: dict[str, Any] = {
            "orderListId": listing.id,
            "contingencyType": "OCO" if listing.kind == "OCO" else "OTO",
            "listStatusType": "EXEC_STARTED" if listing.status == "EXECUTING" else "ALL_DONE",
            "listOrderStatus": listing.status,
            "listClientOrderId": listing.cid,
            "transactionTime": int(time.time() * 1000),
            "symbol": listing.symbol,
            "orders": [
                {"symbol": listing.symbol, "orderId": i, "clientOrderId": self.orders[i].cid}
                for i in listing.order_ids
            ],
        }
        if reports:
            view["orderReports"] = [self._view(self.orders[i]) for i in listing.order_ids]
        return view

    def _find_list(self, params: dict[str, str]) -> FList:
        cid = params.get("listClientOrderId") or params.get("origClientOrderId")
        found = next((item for item in self.lists.values() if item.cid == cid), None)
        if found is None:
            raise ApiError(400, -2013, "Order list does not exist.")
        return found

    def _get_list(self, params: dict[str, str]) -> dict[str, Any]:
        return self._list_view(self._find_list(params))

    def _delete_list(self, params: dict[str, str]) -> dict[str, Any]:
        listing = self._find_list(params)
        if listing.status != "EXECUTING":
            raise ApiError(400, -2011, "Unknown order sent.")
        for oid in listing.order_ids:
            order = self.orders[oid]
            if order.status in ("NEW", "PARTIALLY_FILLED"):
                self._release(order)
                order.status = "CANCELED"
            elif order.status == "PENDING_NEW":
                order.status = "EXPIRED"
        listing.status = "ALL_DONE"
        return self._list_view(listing, reports=True)

    def _stop_kind(self, params: dict[str, str], prefix: str) -> str:
        return params[f"{prefix}Type"]

    def _post_oco(self, params: dict[str, str]) -> dict[str, Any]:
        self._check_list_cid(params["listClientOrderId"])
        qty = D(params["quantity"])
        above = self._new_order(params, kind=params["aboveType"], prefix="above")
        below = self._new_order(params, kind=params["belowType"], prefix="below")
        above.qty = below.qty = qty
        above.side = below.side = "SELL"
        self._check_cid(above.cid)
        self._check_cid(below.cid)
        self._check_filters(above.symbol, qty, above.price)
        price = self.prices[above.symbol]
        if not above.price > price > below.stop:
            raise ApiError(400, -2021, "Order would trigger immediately.")
        self._lock_pair(above, below)
        listing = self._register_list(params, "OCO", [above, below])
        return self._list_view(listing, reports=True)

    def _lock_pair(self, above: FOrder, below: FOrder) -> None:
        asset = above.symbol.removesuffix("USDT")
        if self.free(asset) < above.qty:
            raise ApiError(400, -2010, "Account has insufficient balance for requested action.")
        self.balances[asset][0] -= above.qty
        self.balances[asset][1] += above.qty
        above.locked_base = above.qty
        below.locked_base = _ZERO
        self.orders[above.id] = above
        self.orders[below.id] = below

    def _check_list_cid(self, cid: str) -> None:
        if any(item.cid == cid for item in self.lists.values()):
            raise ApiError(400, -2010, "Duplicate order sent.")

    def _register_list(self, params: dict[str, str], kind: str, orders: list[FOrder]) -> FList:
        listing = FList(
            self._next_id(), params["listClientOrderId"], params["symbol"], kind, [o.id for o in orders]
        )
        for o in orders:
            o.list_id = listing.id
        self.lists[listing.id] = listing
        return listing

    def _post_opoco(self, params: dict[str, str]) -> dict[str, Any]:
        self._check_list_cid(params["listClientOrderId"])
        working = self._new_order(params, kind=params["workingType"], prefix="working")
        working.side, working.qty = params["workingSide"], D(params["workingQuantity"])
        working.price, working.tif = D(params["workingPrice"]), params.get("workingTimeInForce", "GTC")
        above = self._new_order(params, kind=params["pendingAboveType"], prefix="pendingAbove")
        below = self._new_order(params, kind=params["pendingBelowType"], prefix="pendingBelow")
        above.side = below.side = "SELL"
        for o in (working, above, below):
            self._check_cid(o.cid)
        self._check_filters(working.symbol, working.qty, working.price)
        self._lock(working)
        above.status = below.status = "PENDING_NEW"
        for o in (working, above, below):
            self.orders[o.id] = o
        listing = self._register_list(params, "OPOCO", [working, above, below])
        self._match(working.symbol)
        return self._list_view(listing, reports=True)

    def _activate(self, listing: FList) -> None:
        working, above, below = (self.orders[i] for i in listing.order_ids)
        if above.status != "PENDING_NEW":
            return
        price = self.prices[working.symbol]
        base = working.symbol.removesuffix("USDT")
        qty = working.executed - (self._fee_base(working) if self.commission_in_base else _ZERO)
        qty = (qty // self.step) * self.step
        if self.fail_activation or not above.price > price > below.stop or self.free(base) < qty or qty <= 0:
            above.status = below.status = "EXPIRED"
            listing.status = "ALL_DONE"
            return
        above.qty = below.qty = qty
        self.balances[base][0] -= qty
        self.balances[base][1] += qty
        above.locked_base, above.status, below.status = qty, "NEW", "NEW"

    def _fee_base(self, order: FOrder) -> D:
        return order.executed * self.commission_rate

    # ---- motor de execucao ----------------------------------------------------------------------

    def _release(self, order: FOrder) -> None:
        remaining = order.qty - order.executed
        if order.side == "BUY" and order.locked_quote > 0:
            unlock = order.price * remaining
            self.balances["USDT"][1] -= unlock
            self.balances["USDT"][0] += unlock
            order.locked_quote = _ZERO
        if order.side == "SELL" and order.locked_base > 0:
            asset = order.symbol.removesuffix("USDT")
            self.balances[asset][1] -= remaining
            self.balances[asset][0] += remaining
            order.locked_base = _ZERO

    def _fill(self, order: FOrder, qty: D, price: D, *, maker: bool) -> None:
        base = order.symbol.removesuffix("USDT")
        if order.side == "BUY":
            if order.locked_quote > 0:
                self.balances["USDT"][1] -= order.price * qty
                self.balances["USDT"][0] += (order.price - price) * qty
            fee_base = qty * self.commission_rate if self.commission_in_base else _ZERO
            self.balances[base][0] += qty - fee_base
            commission, asset = (
                (fee_base, base)
                if self.commission_in_base
                else (qty * price * self.commission_rate / 600, "BNB")
            )
        else:
            self.balances[base][1] -= qty
            fee = qty * price * self.commission_rate
            self.balances["USDT"][0] += qty * price - fee
            commission, asset = fee, "USDT"
        order.executed += qty
        order.quote += qty * price
        order.status = "FILLED" if order.executed >= order.qty else "PARTIALLY_FILLED"
        self.trades.append(
            {
                "symbol": order.symbol,
                "id": len(self.trades) + 1,
                "orderId": order.id,
                "orderListId": order.list_id,
                "price": _s(price),
                "qty": _s(qty),
                "quoteQty": _s(qty * price),
                "commission": _s(commission),
                "commissionAsset": asset,
                "time": int(time.time() * 1000),
                "isBuyer": order.side == "BUY",
                "isMaker": maker,
                "isBestMatch": True,
            }
        )

    def _after_fill(self, order: FOrder) -> None:
        listing = self.lists.get(order.list_id)
        if listing is None:
            return
        is_working = listing.kind == "OPOCO" and order.id == listing.order_ids[0]
        if is_working and order.status == "FILLED" and not self.hold_activation:
            self._activate(listing)
        legs = listing.order_ids[1:] if listing.kind == "OPOCO" else listing.order_ids
        if not is_working and order.status == "FILLED":
            for other_id in legs:
                other = self.orders[other_id]
                if other.id != order.id and other.status == "NEW":
                    other.locked_base, other.status = _ZERO, "EXPIRED"
            listing.status = "ALL_DONE"

    def _match(self, symbol: str) -> None:
        price = self.prices[symbol]
        for order in list(self.orders.values()):
            if order.symbol != symbol or order.status not in ("NEW", "PARTIALLY_FILLED"):
                continue
            if order.type == "LIMIT" and order.side == "BUY" and price <= order.price:
                remaining = order.qty - order.executed
                fraction, self.fill_fraction = self.fill_fraction, None
                qty = remaining if fraction is None else (remaining * fraction // self.step) * self.step
                if qty > 0:
                    self._fill(order, qty, price, maker=False)
                    self._after_fill(order)
            elif (
                order.type == "LIMIT"
                and order.side == "SELL"
                and order.status == "NEW"
                and price >= order.price
            ):
                self._fill(order, order.qty - order.executed, price, maker=False)
                self._after_fill(order)
            elif order.type == "LIMIT_MAKER" and order.status == "NEW" and price >= order.price:
                self._fill(order, order.qty, order.price, maker=True)
                self._after_fill(order)
            elif (order.type == "STOP_LOSS" and order.status == "NEW" and price <= order.stop) or (
                order.type == "STOP_LOSS_LIMIT"
                and order.status == "NEW"
                and price <= order.stop
                and price >= order.price
            ):
                self._fill(order, order.qty, price, maker=False)
                self._after_fill(order)


class ApiError(Exception):
    def __init__(self, status: int, code: int, msg: str) -> None:
        super().__init__(msg)
        self.status, self.code, self.msg = status, code, msg
