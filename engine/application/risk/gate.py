from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal

from engine.application.risk.approval import _ISSUER, ApprovedEntry, FillGuards
from engine.application.risk.clock import require_aware
from engine.application.risk.filters import (
    EventCalendar,
    MarketSnapshot,
    RunMode,
    check_market,
    history_problem,
)
from engine.application.risk.position import EmergencyExit, ManagedPosition
from engine.application.risk.rejections import Rejection, RejectionCode
from engine.application.risk.sizing import (
    SymbolRules,
    ceil_to_step,
    floor_to_step,
    net_reward_risk_ok,
    size_position,
)
from engine.application.risk.state import RiskState
from engine.domain.candle import Candle
from engine.domain.order import EntryOrder, OrderType
from engine.domain.risk import RiskConfig
from engine.domain.rules import meets_reward_risk
from engine.domain.signal import Signal

LEVERAGED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")
MIN_LEVERAGED_PREFIX = 3


def _reject(code: RejectionCode, message: str) -> Rejection:
    return Rejection(code=code, message=message)


def _split(symbol: str) -> tuple[str, str]:
    base, quote = symbol.split("/")
    return base, quote


def _is_leveraged(base: str) -> bool:
    return any(base.endswith(s) and len(base) - len(s) >= MIN_LEVERAGED_PREFIX for s in LEVERAGED_SUFFIXES)


def asset_problem(symbol: str, config: RiskConfig) -> Rejection | None:
    """So a moeda de cotacao configurada; sem tokens alavancados (a menos que listados na whitelist)."""
    base, quote = _split(symbol)
    if quote != config.quote_asset:
        return _reject(
            RejectionCode.QUOTE_NOT_ALLOWED, f"{symbol}: cotacao {quote} difere de {config.quote_asset}"
        )
    if base in config.blocked_assets:
        return _reject(RejectionCode.ASSET_NOT_ALLOWED, f"{base} esta na lista de bloqueio")
    if config.allowed_assets:
        if base not in config.allowed_assets:
            return _reject(RejectionCode.ASSET_NOT_ALLOWED, f"{base} fora da whitelist de ativos")
    elif _is_leveraged(base):
        return _reject(RejectionCode.ASSET_NOT_ALLOWED, f"{base} parece token alavancado")
    return None


def _position_checks(
    signal: Signal,
    config: RiskConfig,
    positions: Sequence[ManagedPosition],
    pending: Sequence[ApprovedEntry],
    mark: Decimal,
) -> Rejection | None:
    if (same := next((p for p in positions if p.symbol == signal.symbol), None)) is not None:
        if mark < same.entry_price:
            return _reject(
                RejectionCode.ADDING_TO_LOSER, f"{signal.symbol} em prejuizo: proibido aumentar posicao"
            )
        return _reject(RejectionCode.POSITION_ALREADY_OPEN, f"{signal.symbol} ja possui posicao aberta")
    if any(p.order.symbol == signal.symbol for p in pending):
        return _reject(RejectionCode.ENTRY_PENDING, f"{signal.symbol} ja possui ordem de entrada pendente")
    if len(positions) + len(pending) >= config.max_open_positions:
        return _reject(
            RejectionCode.MAX_POSITIONS,
            f"limite de {config.max_open_positions} posicoes (abertas + pendentes)",
        )
    return None


def _signal_problem(signal: Signal, config: RiskConfig, now: datetime) -> Rejection | None:
    age = now - signal.created_at
    if age < timedelta(0):
        return _reject(RejectionCode.INCONSISTENT_INPUT, "sinal com horario no futuro")
    if age > timedelta(seconds=signal.timeframe.seconds) * config.max_signal_age_timeframes:
        return _reject(
            RejectionCode.SIGNAL_STALE,
            f"sinal com {age} de idade; maximo {config.max_signal_age_timeframes} timeframes",
        )
    return None


def _coherence_problem(signal: Signal, market: MarketSnapshot, config: RiskConfig) -> Rejection | None:
    if market.bid <= signal.stop_loss:
        return _reject(
            RejectionCode.MARKET_INCOHERENT, f"bid {market.bid} nao esta acima do stop {signal.stop_loss}"
        )
    deviation = abs(market.mid - signal.entry) / signal.entry
    if deviation > config.max_entry_deviation:
        return _reject(
            RejectionCode.MARKET_INCOHERENT,
            f"preco atual {market.mid} distante {deviation:.4f} da entrada {signal.entry}",
        )
    return None


def evaluate_entry(
    signal: Signal,
    *,
    state: RiskState,
    config: RiskConfig,
    rules: SymbolRules,
    positions: Sequence[ManagedPosition],
    pending: Sequence[ApprovedEntry],
    market: MarketSnapshot,
    history: Sequence[Candle],
    calendar: EventCalendar,
    now: datetime,
    client_order_id: str,
    mode: RunMode = "live",
) -> ApprovedEntry | Rejection:
    """Porta unica das entradas: nenhuma ordem de compra existe sem passar por aqui.

    `pending` sao as aprovacoes ainda sem fill: contam em posicoes maximas, exposicao e duplicidade.
    `mode="backtest"` (replay) dispensa APENAS a cobertura do calendario de eventos; o padrao e `"live"`.
    """
    require_aware(now)
    if signal.symbol != rules.symbol or signal.symbol != market.symbol:
        return _reject(RejectionCode.INCONSISTENT_INPUT, "simbolo do sinal difere das regras ou do mercado")
    if (blocked := asset_problem(signal.symbol, config)) is not None:
        return blocked
    if (blocked := state.entry_block(signal.symbol, now)) is not None:
        return blocked
    if (blocked := _position_checks(signal, config, positions, pending, market.mid)) is not None:
        return blocked
    if (blocked := _signal_problem(signal, config, now)) is not None:
        return blocked
    if problem := history_problem(history, signal.symbol, signal.timeframe, now, config.min_history_candles):
        return _reject(RejectionCode.HISTORY_GAP, problem)
    if (blocked := check_market(market, calendar, config, now, mode=mode)) is not None:
        return blocked
    if (blocked := _coherence_problem(signal, market, config)) is not None:
        return blocked
    if not meets_reward_risk(signal.entry, signal.stop_loss, signal.take_profit, config.min_reward_risk):
        return _reject(RejectionCode.REWARD_RISK, f"retorno/risco abaixo de {config.min_reward_risk}")
    fee = config.taker_fee_rate
    if not net_reward_risk_ok(
        signal.entry, signal.stop_loss, signal.take_profit, fee, config.min_reward_risk
    ):
        return _reject(
            RejectionCode.NET_REWARD_RISK,
            f"retorno/risco liquido de taxas abaixo de {config.min_reward_risk}",
        )

    # arredondamento ao tick que nunca piora o R:R: entrada para baixo, stop e alvo para cima
    tick = rules.tick_size
    try:
        entry = floor_to_step(signal.entry, tick)
        stop = ceil_to_step(signal.stop_loss, tick)
        target = ceil_to_step(signal.take_profit, tick)
    except ValueError as error:
        return _reject(RejectionCode.PRICE_FILTER, f"tick_size incompativel com os niveis: {error}")
    if not 0 < stop < entry < target or not meets_reward_risk(entry, stop, target, config.min_reward_risk):
        return _reject(RejectionCode.PRICE_FILTER, "niveis invalidos apos arredondar ao tick_size")
    if not net_reward_risk_ok(entry, stop, target, fee, config.min_reward_risk):
        return _reject(
            RejectionCode.NET_REWARD_RISK, "retorno/risco liquido de taxas abaixo do minimo apos o tick_size"
        )

    sized = size_position(
        equity=state.equity,
        risk_fraction=config.risk_per_trade,
        max_position_fraction=config.max_position_fraction,
        entry=entry,
        stop_loss=stop,
        rules=rules,
        fee_rate=fee,
    )
    if isinstance(sized, Rejection):
        return sized

    held = [(p.symbol, p.quantity * p.entry_price) for p in positions]
    held += [(p.order.symbol, p.order.quantity * p.order.price) for p in pending]
    new_notional = sized.quantity * entry
    base = _split(signal.symbol)[0]
    coin = sum((v for s, v in held if _split(s)[0] == base), start=Decimal(0))
    if coin + new_notional > config.max_coin_exposure_fraction * state.equity:
        return _reject(
            RejectionCode.COIN_EXPOSURE, f"exposicao em {base} acima de {config.max_coin_exposure_fraction}"
        )
    total = sum((v for _, v in held), start=Decimal(0))
    if total + new_notional > config.max_total_exposure_fraction * state.equity:
        return _reject(
            RejectionCode.TOTAL_EXPOSURE,
            f"exposicao total acima de {config.max_total_exposure_fraction} do capital",
        )

    order = EntryOrder(
        client_order_id=client_order_id,
        symbol=signal.symbol,
        type=OrderType.LIMIT,
        quantity=sized.quantity,
        price=entry,
        stop_loss=stop,
        take_profit=target,
        created_at=signal.created_at,
    )
    ttl = timedelta(seconds=signal.timeframe.seconds) * config.max_signal_age_timeframes
    guards = FillGuards(
        approved_at=now,
        expires_at=now + ttl,
        max_fee_rate=config.max_entry_fee_multiple * fee,
        min_quantity=rules.min_quantity,
        min_notional=rules.min_notional,
    )
    return ApprovedEntry(order, signal, sized.risk_amount, guards=guards, _issuer=_ISSUER)


def expire_pending(
    pending: Sequence[ApprovedEntry], now: datetime
) -> tuple[tuple[ApprovedEntry, ...], tuple[ApprovedEntry, ...]]:
    """Separa as aprovacoes pendentes em (ativas, expiradas) no instante `now`.

    O executor DEVE chamar isto antes de cada `evaluate_entry` e a cada ciclo: para cada aprovacao
    expirada, cancelar a ordem limite na corretora (e conferir se houve fill de ultima hora, tratado por
    `open_position`) e so entao descarta-la. Sem isso, aprovacoes mortas ocupam vagas em `max_open_positions`,
    exposicao e duplicidade (`ENTRY_PENDING`) indefinidamente.
    """
    require_aware(now)
    active = tuple(p for p in pending if not p.is_expired(now))
    return active, tuple(p for p in pending if p.is_expired(now))


def _emergency(
    order: EntryOrder,
    price: Decimal,
    quantity: Decimal,
    fee: Decimal,
    opened_at: datetime,
    reasons: list[str],
) -> EmergencyExit:
    return EmergencyExit(
        symbol=order.symbol,
        quantity=quantity,
        fill_price=price,
        stop_loss=order.stop_loss,
        entry_fee=fee,
        opened_at=opened_at,
        reason="; ".join(reasons),
    )


def open_position(
    approved: ApprovedEntry,
    *,
    opened_at: datetime,
    fill_price: Decimal | None = None,
    filled_quantity: Decimal | None = None,
    fee: Decimal = Decimal(0),
) -> ManagedPosition | EmergencyExit | Rejection:
    """Posicao a partir do fill real (`opened_at` = horario do fill). O fill JA aconteceu: a moeda esta na
    carteira, entao qualquer anomalia com quantidade comprada vira resultado explicito, nunca excecao.

    Contrato de retorno:
    - `ManagedPosition`: fill dentro do aprovado (preco <= limite, quantidade <= aprovada, acima do stop,
      dentro da validade e do teto de taxa).
    - `EmergencyExit`: fill fora do aprovado (no stop ou abaixo, apos `guards.expires_at`, taxa acima do
      teto `max_entry_fee_multiple` x taker, preco acima do limite ou quantidade acima da aprovada). O
      executor DEVE vender a mercado de imediato (acao "SELL_MARKET_IMMEDIATELY") e registrar o resultado
      real via `record_trade`; `reason` lista os motivos.
    - `Rejection` (QUANTITY/NOTIONAL_BELOW_MIN): fill em poeira, abaixo dos minimos da corretora (nao da para
      proteger nem vender); o executor trata o saldo como poeira.
    Levanta `ValueError` apenas para dado estruturalmente corrompido, sem quantidade comprada confiavel:
    preco/quantidade nao finitos ou <= 0, taxa negativa/nao finita, horario sem fuso ou anterior a aprovacao;
    e `ApprovalError` se a aprovacao nao e autentica.
    """
    approved = approved.verify()
    order = approved.order
    guards = approved.guards
    price = order.price if fill_price is None else fill_price
    quantity = order.quantity if filled_quantity is None else filled_quantity
    if not price.is_finite() or price <= 0:
        raise ValueError(f"fill_price {price} invalido")
    if not quantity.is_finite() or quantity <= 0:
        raise ValueError(f"filled_quantity {quantity} invalida")
    if not fee.is_finite() or fee < 0:
        raise ValueError("fee must be a non-negative finite value")
    require_aware(opened_at, "opened_at")
    if opened_at < guards.approved_at:
        raise ValueError("opened_at precede a aprovacao")
    if quantity < guards.min_quantity or quantity * price < guards.min_notional:
        code = (
            RejectionCode.QUANTITY_BELOW_MIN
            if quantity < guards.min_quantity
            else RejectionCode.NOTIONAL_BELOW_MIN
        )
        return Rejection(
            code=code,
            message=f"fill em poeira: {quantity} @ {price} abaixo dos minimos da corretora",
        )
    anomalies: list[str] = []
    if approved.is_expired(opened_at):
        anomalies.append(f"aprovacao expirada em {guards.expires_at.isoformat()}")
    if fee > guards.max_fee_rate * price * quantity:
        anomalies.append(f"taxa anormal {fee} acima do teto de {guards.max_fee_rate} do notional")
    if price > order.price:
        anomalies.append(f"preco {price} acima do limite aprovado {order.price}")
    if quantity > order.quantity:
        anomalies.append(f"quantidade {quantity} acima da aprovada {order.quantity}")
    if price <= order.stop_loss:
        anomalies.append(f"fill {price} no stop aprovado {order.stop_loss} ou abaixo")
    if anomalies:
        return _emergency(order, price, quantity, fee, opened_at, anomalies)
    return ManagedPosition(
        symbol=order.symbol,
        entry_price=price,
        initial_quantity=quantity,
        quantity=quantity,
        initial_stop=order.stop_loss,
        stop=order.stop_loss,
        take_profit=order.take_profit,
        highest_price=price,
        entry_fee=fee,
        opened_at=opened_at,
    )
