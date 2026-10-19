import contextlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from engine.application.backtest.hypothetical import simulate_signal
from engine.application.backtest.models import (
    BacktestInvariantError,
    BacktestResult,
    DecisionAction,
    DecisionRecord,
    EquityPoint,
    RoundTrip,
)
from engine.application.backtest.settings import BacktestSettings, default_rules
from engine.application.risk import (
    ApprovedEntry,
    EmergencyExit,
    EventCalendar,
    ExitFill,
    ManagedPosition,
    MarketSnapshot,
    Rejection,
    RiskState,
    SymbolRules,
    close_position,
    evaluate_entry,
    expire_pending,
    manage_position,
    open_position,
)
from engine.application.risk.sizing import floor_to_step, meets_minimums
from engine.application.strategies import Strategy, StrategyContext, StrategySignal
from engine.domain.candle import Candle
from engine.domain.order import ExitOrder, OrderStatus, OrderType
from engine.domain.signal import Signal
from engine.domain.trade import ExitReason, Trade
from engine.infrastructure.backtest import BacktestGateway, Fill, UnknownOrderError

Clock = Callable[[], datetime]

_OCO, _PARTIAL, _MARKET_EXIT, _EMERGENCY = "oco", "partial", "x", "emg"
# a foto de equity precede a fronteira da vela: fills a mercado na abertura da proxima vela (mesmo instante)
# ainda contam como posteriores a foto, sem serem descartados por `RiskState.record_trade`
_PHOTO_LAG = timedelta(microseconds=1)
_RECONCILE_TOLERANCE = Decimal("0.000001")
_DAY_SECONDS = 86_400


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class _Pending:
    approved: ApprovedEntry
    strategy: str
    confidence: float


@dataclass
class _Live:
    entry_id: str
    strategy: str
    approved: ApprovedEntry
    opened_index: int
    position: ManagedPosition | None
    emergency: EmergencyExit | None = None
    gateway_stop: Decimal = Decimal(0)
    partial_id: str | None = None
    partial_fill: Fill | None = None
    closing: ExitFill | None = None
    exit_seq: int = 0
    legs: list[Trade] = field(default_factory=list)
    last_leg_intra: bool = False


@dataclass(frozen=True)
class _Veto:
    decision_index: int
    signal: Signal
    index: int
    equity: Decimal


class BacktestEngine:
    """Roda vela a vela o mesmo pipeline do Paper/Live sobre o `BacktestGateway`, para UM simbolo/timeframe.

    Em cada vela fechada i (instante `close_time`): o gateway executa o que as velas ate i permitem; os fills
    viram posicoes/trades (`open_position`, `manage_position`, `RiskState`); so entao as estrategias veem
    apenas velas <= i e as ordens aprovadas so executam a partir da vela i + latencia. Nenhuma decisao le o
    futuro; as operacoes evitadas sao simuladas ao final, somente para o relatorio.

    A gestao de posicao (parcial no 1o alvo, breakeven, trailing, protecao de lucro) e a do modulo de risco;
    o gateway executa: stop (OCO sem alvo, subido via `raise_stop`), parcial como limit de venda no alvo e
    saidas por protecao de lucro como venda a mercado na abertura seguinte. Taxas, precos e horarios dos
    trades vem dos fills do gateway. Posicoes ainda abertas no fim dos dados NAO viram trade: entram na
    curva de equity marcada a mercado e em `open_at_end`.
    """

    def __init__(
        self,
        strategies: Sequence[Strategy],
        settings: BacktestSettings | None = None,
        *,
        rules: SymbolRules | None = None,
        calendar: EventCalendar | None = None,
        clock: Clock = _utc_now,
    ) -> None:
        if not strategies:
            raise ValueError("at least one strategy is required")
        self.strategies = tuple(strategies)
        self.settings = settings or BacktestSettings()
        self.rules = rules
        self.calendar = calendar or EventCalendar()
        self.clock = clock

    async def run(self, candles: Sequence[Candle], *, start: int = 0) -> BacktestResult:
        """`candles[:start]` e so aquecimento dos indicadores; o backtest roda de `start` ate o fim."""
        if not 0 <= start < len(candles):
            raise ValueError("start must index a candle")
        rules = self.rules or default_rules(candles[start].symbol)
        return await _Session(self, list(candles), start, rules).execute()


class _Session:
    def __init__(self, engine: BacktestEngine, candles: list[Candle], start: int, rules: SymbolRules) -> None:
        self.engine = engine
        self.settings = engine.settings
        self.risk = engine.settings.risk
        self.rules = rules
        self.candles = candles
        self.start = start
        self.gateway = BacktestGateway(candles[start:], self.settings.gateway_config())
        first = candles[start]
        self.symbol = first.symbol
        self.timeframe = first.timeframe
        self.state = RiskState.initial(self.settings.initial_capital, first.open_time)
        self.context = StrategyContext(taker_fee_rate=self.risk.taker_fee_rate)
        self.pending: dict[str, _Pending] = {}
        self.live: dict[str, _Live] = {}
        self.decisions: list[DecisionRecord] = []
        self.vetoes: list[_Veto] = []
        self.round_trips: list[RoundTrip] = []
        self.trades: list[Trade] = []
        self.curve: list[EquityPoint] = []
        self.seen_fills = 0
        self.approvals = 0
        self.no_signal = 0
        self.index = start - 1

    async def execute(self) -> BacktestResult:
        while self.gateway.has_next:
            # limite aprovado so pode executar em velas que abrem antes de `expires_at`: cancela antes de
            # a proxima vela ser executada (equivale a `expire_pending` em open_time + 1us)
            await self._expire_pending(self.candles[self.index + 1].open_time + _PHOTO_LAG)
            if (candle := self.gateway.advance()) is None:
                break
            self.index += 1
            now = candle.close_time
            await self._process_fills()
            await self._manage_positions(candle)
            self.state = self.state.roll_day(now)
            await self._photo(now)
            await self._expire_pending(now)
            self.curve.append(self._equity_point(candle))
            if self.gateway.has_next:
                await self._evaluate_signals(candle)
        return self._result()

    async def _process_fills(self) -> None:
        fills = self.gateway.fills[self.seen_fills :]
        self.seen_fills += len(fills)
        stopped_now = {f.client_order_id.partition(":")[0] for f in fills if f.client_order_id.endswith(_OCO)}
        for fill in fills:
            entry_id, _, leg = fill.client_order_id.partition(":")
            if fill.side == "buy":
                await self._on_entry_fill(fill, entry_id in stopped_now)
            elif leg == _OCO:
                await self._on_protective_fill(self.live[entry_id], fill)
            elif leg == _PARTIAL:
                self.live[entry_id].partial_fill = fill
            else:
                self._on_exit_fill(self.live[entry_id], fill, leg)

    async def _on_entry_fill(self, fill: Fill, stopped_now: bool) -> None:
        entry_id = fill.client_order_id
        pending = self.pending.pop(entry_id)
        outcome = open_position(
            pending.approved,
            opened_at=fill.at,
            fill_price=fill.price,
            filled_quantity=fill.quantity,
            fee=fill.fee,
        )
        match outcome:
            case ManagedPosition() as position:
                live = _Live(
                    entry_id,
                    pending.strategy,
                    pending.approved,
                    self.index,
                    position,
                    gateway_stop=position.stop,
                )
                self.live[entry_id] = live
                if not stopped_now:
                    await self._protect(live, position)
            case EmergencyExit() as emergency:
                live = _Live(entry_id, pending.strategy, pending.approved, self.index, None, emergency)
                self.live[entry_id] = live
                self._record(
                    pending.strategy,
                    DecisionAction.TRADED,
                    "emergency_exit",
                    f"fill fora do aprovado, venda a mercado imediata: {emergency.reason}",
                    pending.approved.signal,
                    pending.confidence,
                    entry_id,
                )
                if not stopped_now:
                    await self._sell_market(emergency.quantity, entry_id, _EMERGENCY)
            case Rejection() as dust:
                raise BacktestInvariantError(f"fill em poeira impossivel no gateway simulado: {dust.message}")

    async def _protect(self, live: _Live, position: ManagedPosition) -> None:
        """Stop puro no OCO; o alvo vira um limit de venda do tamanho da parcial de `manage_position`."""
        self.gateway.drop_target(live.entry_id)
        target = position.take_profit
        part = floor_to_step(position.quantity * self.risk.partial_exit_fraction, self.rules.step_size)
        rest = position.quantity - part
        viable = target is not None and all(
            meets_minimums(quantity, price, self.rules) is None
            for quantity, price in ((part, target), (rest, target), (rest, position.stop))
        )
        if not viable or target is None:
            # mesmo estado a que `manage_position` chegaria sem poder dividir: trailing na posicao inteira
            live.position = position.model_copy(update={"partial_done": True, "take_profit": None})
            return
        order = ExitOrder(
            client_order_id=f"{live.entry_id}:{_PARTIAL}",
            symbol=self.symbol,
            type=OrderType.LIMIT,
            quantity=part,
            price=target,
            created_at=self._now(),
        )
        report = await self.gateway.place_order(order)
        if report.status is OrderStatus.REJECTED:
            reason = self.gateway.rejection_reason(order.client_order_id)
            raise BacktestInvariantError(f"parcial recusada pelo gateway: {reason}")
        live.partial_id = order.client_order_id

    async def _sell_market(self, quantity: Decimal, entry_id: str, leg: str) -> None:
        order = ExitOrder(
            client_order_id=f"{entry_id}:{leg}",
            symbol=self.symbol,
            type=OrderType.MARKET,
            quantity=quantity,
            created_at=self._now(),
        )
        report = await self.gateway.place_order(order)
        if report.status is OrderStatus.REJECTED:
            reason = self.gateway.rejection_reason(order.client_order_id)
            raise BacktestInvariantError(f"venda a mercado recusada pelo gateway: {reason}")

    async def _on_protective_fill(self, live: _Live, fill: Fill) -> None:
        await self._cancel_partial(live)
        reason = fill.exit_reason or ExitReason.STOP_LOSS
        if live.position is not None:
            if fill.quantity != live.position.quantity:
                raise BacktestInvariantError("quantidade do OCO diverge da posicao")
            at = max(fill.at, live.position.opened_at)
            exit_fill = close_position(live.position, fill.price, at, reason, fee=fill.fee)
            trade = exit_fill.to_trade(live.position)
        else:
            trade = self._emergency_trade(live, fill, reason)
        self._finish(live, trade)

    def _on_exit_fill(self, live: _Live, fill: Fill, leg: str) -> None:
        if leg == _EMERGENCY and live.emergency is not None:
            self._finish(live, self._emergency_trade(live, fill, ExitReason.MANUAL))
            return
        if live.closing is None or live.position is None or fill.quantity != live.closing.quantity:
            raise BacktestInvariantError(f"fill de saida inesperado: {fill.client_order_id}")
        at = max(fill.at, live.position.opened_at)
        exit_fill = live.closing.model_copy(update={"price": fill.price, "fee": fill.fee, "at": at})
        self._finish(live, exit_fill.to_trade(live.position))

    def _emergency_trade(self, live: _Live, fill: Fill, reason: ExitReason) -> Trade:
        emergency = live.emergency
        if emergency is None:
            raise BacktestInvariantError("trade de emergencia sem EmergencyExit")
        return Trade(
            trade_id=f"{live.entry_id}:{_EMERGENCY}",
            symbol=emergency.symbol,
            quantity=emergency.quantity,
            entry_price=emergency.fill_price,
            exit_price=fill.price,
            entry_fee=emergency.entry_fee,
            exit_fee=fill.fee,
            opened_at=emergency.opened_at,
            closed_at=max(fill.at, emergency.opened_at),
            exit_reason=reason,
        )

    def _leg(self, live: _Live, trade: Trade) -> None:
        live.legs.append(trade)
        self.trades.append(trade)
        # saida intra-vela carrega `close_time` (00:00 na ultima vela do dia): pertence a vela que fechou,
        # logo ao dia anterior, como a foto de equity; fills de abertura pertencem a vela que abre
        live.last_leg_intra = trade.closed_at == self.gateway.now
        now = trade.closed_at - _PHOTO_LAG if live.last_leg_intra else trade.closed_at
        self.state = self.state.record_trade(trade, now, self.risk)

    def _finish(self, live: _Live, trade: Trade) -> None:
        self._leg(live, trade)
        del self.live[live.entry_id]
        legs = live.legs
        pnl = sum((t.pnl for t in legs), Decimal(0))
        risk_amount = live.approved.risk_amount
        self.round_trips.append(
            RoundTrip(
                order_id=live.entry_id,
                symbol=self.symbol,
                strategy=live.strategy,
                opened_at=legs[0].opened_at,
                closed_at=legs[-1].closed_at,
                entry_price=legs[0].entry_price,
                quantity=live.approved.order.quantity,
                pnl=pnl,
                fees=sum((t.entry_fee + t.exit_fee for t in legs), Decimal(0)),
                risk_amount=risk_amount,
                r_multiple=pnl / risk_amount,
                exit_reasons=tuple(t.exit_reason for t in legs),
                exit_labels=self._exit_labels(live),
                closed_intra_candle=live.last_leg_intra,
                legs=len(legs),
            )
        )

    def _exit_labels(self, live: _Live) -> tuple[str, ...]:
        """Rotulo so de relatorio: stop movido a favor com saida lucrativa nao aparece como `stop_loss`."""
        labels = [t.exit_reason.value for t in live.legs]
        last = live.legs[-1]
        moved = live.gateway_stop > live.approved.order.stop_loss
        if last.exit_reason is ExitReason.STOP_LOSS and moved and last.pnl > 0:
            trailing = len(live.legs) > 1 or (live.position is not None and live.position.partial_done)
            labels[-1] = "trailing_stop" if trailing else "breakeven_stop"
        return tuple(labels)

    async def _cancel_partial(self, live: _Live) -> None:
        if live.partial_id is not None:
            with contextlib.suppress(UnknownOrderError):
                await self.gateway.cancel_order(self.symbol, live.partial_id)

    async def _manage_positions(self, candle: Candle) -> None:
        for live in list(self.live.values()):
            position = live.position
            if position is None or live.closing is not None:
                continue
            # o limit da parcial so fica ativo no gateway a partir de opened_index + latencia; ate la so o
            # stop (ativo desde o fill) protege e a gestao do alvo/trailing nao avalia velas
            if self.index < live.opened_index + self.settings.latency_candles:
                continue
            result = manage_position(position, candle, self.rules, self.risk)
            exits = list(result.exits)
            partial = next((x for x in exits if x.reason is ExitReason.TAKE_PROFIT), None)
            if (partial is None) != (live.partial_fill is None):
                raise BacktestInvariantError("parcial da gestao e do gateway divergem")
            if partial is not None and live.partial_fill is not None:
                fill = live.partial_fill
                if fill.quantity != partial.quantity:
                    raise BacktestInvariantError("quantidade da parcial diverge entre gestao e gateway")
                at = max(fill.at, position.opened_at)
                real = partial.model_copy(update={"price": fill.price, "fee": fill.fee, "at": at})
                self._leg(live, real.to_trade(position))
                exits.remove(partial)
                live.partial_fill = None
                live.partial_id = None
            if result.position is None:
                await self._begin_exit(live, position, candle, exits)
                continue
            live.position = result.position
            if result.stop_update is not None and result.stop_update > live.gateway_stop:
                self.gateway.raise_stop(live.entry_id, result.stop_update)
                live.gateway_stop = result.stop_update

    async def _begin_exit(
        self, live: _Live, position: ManagedPosition, candle: Candle, exits: list[ExitFill]
    ) -> None:
        if len(exits) != 1 or candle.low <= position.stop:
            raise BacktestInvariantError("gestao pede saida por stop que o gateway nao executou")
        [final] = exits
        await self._cancel_partial(live)
        live.partial_id = None
        live.exit_seq += 1
        live.closing = final
        await self._sell_market(final.quantity, live.entry_id, f"{_MARKET_EXIT}{live.exit_seq}")

    async def _photo(self, now: datetime) -> None:
        """Foto autoritativa do capital: so sem posicao aberta, ou seja, apenas com trades ja fechados."""
        if self.live:
            return
        balance = (await self.gateway.get_balances())[self.symbol.split("/")[1]]
        cash = balance.free + balance.locked
        if abs(cash - self.state.equity) > _RECONCILE_TOLERANCE:
            raise BacktestInvariantError(f"capital do gateway {cash} diverge do ledger {self.state.equity}")
        self.state = self.state.observe_equity(cash, now - _PHOTO_LAG, self.risk)

    async def _expire_pending(self, now: datetime) -> None:
        _, expired = expire_pending(tuple(p.approved for p in self.pending.values()), now)
        for approved in expired:
            order_id = approved.order.client_order_id
            await self.gateway.cancel_order(self.symbol, order_id)
            pending = self.pending.pop(order_id)
            self._record(
                pending.strategy,
                DecisionAction.SKIPPED,
                "entry_expired",
                "ordem limite aprovada nao executou dentro da validade e foi cancelada",
                approved.signal,
                pending.confidence,
                order_id,
            )

    def _equity_point(self, candle: Candle) -> EquityPoint:
        fee = self.risk.taker_fee_rate
        equity = self.state.equity
        for live in self.live.values():
            if (position := live.position) is not None:
                equity += position.unrealized_pnl(candle.close) - position.quantity * candle.close * fee
            elif (em := live.emergency) is not None:
                gross = (candle.close - em.fill_price) * em.quantity
                equity += gross - em.entry_fee - em.quantity * candle.close * fee
        return EquityPoint(
            at=candle.close_time, equity=equity, realized=self.state.equity, in_position=bool(self.live)
        )

    async def _evaluate_signals(self, candle: Candle) -> None:
        window = self.candles[max(0, self.index - self.settings.history_window + 1) : self.index + 1]
        found: list[StrategySignal] = []
        for strategy in self.engine.strategies:
            if (signal := strategy.evaluate(window, self.context)) is None:
                self.no_signal += 1
            else:
                found.append(signal)
        found.sort(key=lambda s: (-s.confidence, s.signal.strategy))
        for result in found:
            await self._decide(result, window, candle)

    async def _decide(self, result: StrategySignal, window: list[Candle], candle: Candle) -> None:
        signal = result.signal
        name = signal.strategy
        now = candle.close_time
        if any(live.emergency is not None for live in self.live.values()):
            message = "venda de emergencia em andamento"
            self._record(
                name, DecisionAction.SKIPPED, "emergency_exit_pending", message, signal, result.confidence
            )
            return
        steps = max(1, _DAY_SECONDS // self.timeframe.seconds)
        market = MarketSnapshot(
            symbol=self.symbol,
            at=now,
            bid=candle.close,
            ask=candle.close,
            quote_volume_24h=sum((c.close * c.volume for c in window[-steps:]), Decimal(0)),
        )
        outcome = evaluate_entry(
            signal,
            state=self.state,
            config=self.risk,
            rules=self.rules,
            positions=[p for live in self.live.values() if (p := live.position) is not None],
            pending=[p.approved for p in self.pending.values()],
            market=market,
            history=window,
            calendar=self.engine.calendar,
            now=now,
            client_order_id=f"bt{self.approvals + 1:06d}",
            mode="backtest",
        )
        if isinstance(outcome, Rejection):
            self._veto(name, outcome.code.value, outcome.message, signal, result.confidence)
            return
        self.approvals += 1
        order_id = outcome.order.client_order_id
        report = await self.gateway.place_order(outcome.order)
        if report.status is OrderStatus.REJECTED:
            reason = self.gateway.rejection_reason(order_id) or "recusada"
            self._veto(
                name, "gateway_rejected", f"gateway recusou a ordem: {reason}", signal, result.confidence
            )
            return
        self.pending[order_id] = _Pending(outcome, name, result.confidence)
        reason = outcome.to_decision().reason
        self._record(name, DecisionAction.TRADED, "approved", reason, signal, result.confidence, order_id)

    def _veto(self, strategy: str, code: str, message: str, signal: Signal, confidence: float) -> None:
        self._record(strategy, DecisionAction.SKIPPED, code, message, signal, confidence)
        self.vetoes.append(_Veto(len(self.decisions) - 1, signal, self.index, self.state.equity))

    def _record(
        self,
        strategy: str,
        action: DecisionAction,
        code: str,
        message: str,
        signal: Signal,
        confidence: float,
        order_id: str | None = None,
    ) -> None:
        self.decisions.append(
            DecisionRecord(
                at=self._now(),
                strategy=strategy,
                symbol=self.symbol,
                action=action,
                code=code,
                message=message,
                confidence=confidence,
                entry=signal.entry,
                stop=signal.stop_loss,
                target=signal.take_profit,
                order_id=order_id,
            )
        )

    def _now(self) -> datetime:
        now = self.gateway.now
        if now is None:
            raise BacktestInvariantError("relogio do gateway ainda nao iniciado")
        return now

    def _result(self) -> BacktestResult:
        decisions = list(self.decisions)
        for veto in self.vetoes:
            outcome = simulate_signal(
                veto.signal,
                self.candles,
                veto.index,
                equity=veto.equity,
                settings=self.settings,
                rules=self.rules,
            )
            decisions[veto.decision_index] = decisions[veto.decision_index].model_copy(
                update={"hypothetical": outcome}
            )
        return BacktestResult(
            symbol=self.symbol,
            timeframe=self.timeframe.value,
            strategies=tuple(s.name for s in self.engine.strategies),
            settings=self.settings,
            generated_at=self.engine.clock(),
            period_start=self.candles[self.start].open_time,
            period_end=self.candles[-1].close_time,
            candles=len(self.candles) - self.start,
            initial_capital=self.settings.initial_capital,
            final_equity=self.curve[-1].equity,
            final_realized_equity=self.state.equity,
            round_trips=tuple(self.round_trips),
            trades=tuple(self.trades),
            decisions=tuple(decisions),
            steps_without_signal=self.no_signal,
            equity_curve=tuple(self.curve),
            risk_events=self.state.audit,
            halted=self.state.halted,
            open_at_end=len(self.live),
            total_slippage_cost=sum((f.slippage_cost for f in self.gateway.fills), Decimal(0)),
        )
