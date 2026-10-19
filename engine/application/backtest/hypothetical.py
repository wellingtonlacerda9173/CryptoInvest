from collections.abc import Sequence
from decimal import Decimal

from engine.application.backtest.models import HypotheticalOutcome, HypotheticalStatus
from engine.application.backtest.settings import BacktestSettings
from engine.application.risk import Rejection, SymbolRules
from engine.application.risk.sizing import size_position
from engine.domain.candle import Candle
from engine.domain.signal import Signal

_BPS = Decimal(10_000)


def simulate_signal(
    signal: Signal,
    candles: Sequence[Candle],
    index: int,
    *,
    equity: Decimal,
    settings: BacktestSettings,
    rules: SymbolRules,
) -> HypotheticalOutcome:
    """Resultado hipotetico de um sinal NAO operado, lendo o futuro apenas para o relatorio.

    Mesmas regras pessimistas do gateway: limite na entrada (janela de validade da aprovacao), na vela de
    entrada so vale o stop, stop antes do alvo na mesma vela, gap no stop executa na abertura, slippage contra
    o trader. Tamanho = `size_position` do gate (risco por operacao E teto de posicao) sobre o capital no
    instante do sinal; recusado por minimos da corretora (poeira) => `NOT_SIMULATED`. Sem parciais/trailing:
    sai no alvo, no stop ou, ao fim de `hypothetical_horizon` velas, a mercado no fechamento. Cada sinal e
    simulado de forma independente (capital do instante do sinal, sem competir por posicao).
    """
    risk = settings.risk
    entry, stop, target = signal.entry, signal.stop_loss, signal.take_profit
    slip = settings.slippage_bps / _BPS
    taker, maker = risk.taker_fee_rate, settings.maker_fee_rate
    sized = size_position(
        equity=equity,
        risk_fraction=risk.risk_per_trade,
        max_position_fraction=risk.max_position_fraction,
        entry=entry,
        stop_loss=stop,
        rules=rules,
        fee_rate=taker,
    )
    if isinstance(sized, Rejection):
        return HypotheticalOutcome(
            status=HypotheticalStatus.NOT_SIMULATED, pnl=Decimal(0), r_multiple=Decimal(0), candles=0
        )
    quantity, risk_amount = sized.quantity, sized.risk_amount
    last = len(candles) - 1

    fill_at = next(
        (
            j
            for j in range(
                index + settings.latency_candles, min(index + risk.max_signal_age_timeframes, last) + 1
            )
            if candles[j].low <= entry
        ),
        None,
    )
    if fill_at is None:
        ended = index + risk.max_signal_age_timeframes > last
        status = HypotheticalStatus.UNRESOLVED if ended else HypotheticalStatus.NOT_FILLED
        return HypotheticalOutcome(status=status, pnl=Decimal(0), r_multiple=Decimal(0), candles=0)

    def outcome(
        status: HypotheticalStatus, exit_price: Decimal, fee: Decimal, at: int
    ) -> HypotheticalOutcome:
        pnl = (exit_price - entry) * quantity - entry * quantity * maker - exit_price * quantity * fee
        return HypotheticalOutcome(status=status, pnl=pnl, r_multiple=pnl / risk_amount, candles=at - fill_at)

    def stopped(candle: Candle, at: int) -> HypotheticalOutcome:
        return outcome(HypotheticalStatus.STOP, min(stop, candle.open) * (1 - slip), taker, at)

    if candles[fill_at].low <= stop:
        return stopped(candles[fill_at], fill_at)
    end = min(fill_at + settings.hypothetical_horizon, last)
    for k in range(fill_at + 1, end + 1):
        candle = candles[k]
        if candle.low <= stop:
            return stopped(candle, k)
        if candle.high >= target:
            return outcome(HypotheticalStatus.TARGET, target, maker, k)
    status = (
        HypotheticalStatus.TIMEOUT
        if fill_at + settings.hypothetical_horizon <= last
        else HypotheticalStatus.UNRESOLVED
    )
    return outcome(status, candles[end].close * (1 - slip), taker, end)
