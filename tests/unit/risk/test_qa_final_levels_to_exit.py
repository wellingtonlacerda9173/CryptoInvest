"""Verificacao final: niveis das estrategias -> gate -> posicao -> saida, com varias taxas, precos e ticks."""

import random
from datetime import timedelta
from decimal import Decimal

import pytest

from engine.application.risk import (
    ApprovedEntry,
    ManagedPosition,
    Rejection,
    RiskState,
    SymbolRules,
    evaluate_entry,
    ignored_assets_for,
    manage_position,
    open_position,
)
from engine.application.strategies.levels import LevelsConfig, StopMode, compute_levels
from engine.domain.candle import Candle
from engine.domain.risk import RiskConfig
from engine.domain.signal import Signal
from engine.domain.trade import ExitReason
from engine.domain.types import Timeframe
from tests.unit.risk.factories import EMPTY_CALENDAR, NOW, SYMBOL, history, market

D = Decimal


def _scenario(seed: int) -> tuple[RiskConfig, SymbolRules, Signal] | None:
    rng = random.Random(seed)  # noqa: S311
    entry = D(repr(round(10 ** rng.uniform(-1, 5), rng.choice([2, 4, 8]))))
    fee = D(repr(round(rng.uniform(0, 0.01), 5)))
    levels = compute_levels(
        entry,
        float(entry) * rng.uniform(0.002, 0.05),
        entry * D(repr(1 - rng.uniform(0.001, 0.08))),
        LevelsConfig(stop_mode=rng.choice(list(StopMode)), reward_risk=D(repr(round(rng.uniform(2, 4), 2)))),
        fee,
    )
    if levels is None:
        return None
    stop, target = levels
    signal = Signal(
        symbol=SYMBOL,
        timeframe=Timeframe.H1,
        created_at=NOW,
        strategy="fuzz",
        entry=entry,
        stop_loss=stop,
        take_profit=target,
    )
    rules = SymbolRules(
        symbol=SYMBOL,
        tick_size=D("0.00000001"),
        step_size=D("0.00000001"),
        min_quantity=D("0.00000001"),
        min_notional=D(1),
    )
    return RiskConfig(taker_fee_rate=fee), rules, signal


def _candle(order_symbol: str, at_open: object, **prices: Decimal) -> Candle:
    return Candle(
        symbol=order_symbol,
        timeframe=Timeframe.M15,
        open_time=at_open,  # type: ignore[arg-type]
        volume=D(1),
        closed=True,
        **prices,
    )


@pytest.mark.parametrize("seed", [s for s in range(160) if _scenario(s) is not None][:100])
def test_dado_nivel_da_estrategia_quando_stop_ou_alvo_entao_perda_limitada_e_ganho_liquido_2r(
    seed: int,
) -> None:
    built = _scenario(seed)
    assert built is not None
    config, rules, signal = built
    approved = evaluate_entry(
        signal,
        state=RiskState.initial(D(10_000), NOW),
        config=config,
        rules=rules,
        positions=[],
        pending=[],
        market=market(bid=str(signal.entry), ask=str(signal.entry)),
        history=history(),
        calendar=EMPTY_CALENDAR,
        now=NOW,
        client_order_id=f"fz-{seed}",
    )
    assert not isinstance(approved, Rejection), approved
    assert isinstance(approved, ApprovedEntry)
    order = approved.order
    entry_fee = config.taker_fee_rate * order.price * order.quantity
    position = open_position(approved, opened_at=NOW, fee=entry_fee)
    assert isinstance(position, ManagedPosition)
    start = NOW + timedelta(minutes=1)

    stopped = manage_position(
        position,
        _candle(
            order.symbol,
            start,
            open=order.price,
            high=order.price,
            low=order.stop_loss,
            close=order.stop_loss,
        ),
        rules,
        config,
    )
    [stop_fill] = stopped.exits
    reached = manage_position(
        position,
        _candle(
            order.symbol,
            start,
            open=order.price,
            high=order.take_profit,
            low=order.price - (order.price - order.stop_loss) / 2,
            close=order.take_profit,
        ),
        rules,
        config,
    )
    [target_fill] = reached.exits
    share = approved.risk_amount * target_fill.quantity / order.quantity

    assert -stop_fill.to_trade(position).pnl <= approved.risk_amount
    assert (target_fill.reason, target_fill.price) == (ExitReason.TAKE_PROFIT, order.take_profit)
    assert target_fill.to_trade(position).pnl >= 2 * share


def test_bnb_bloqueado_mesmo_na_whitelist_continua_ignorado_na_reconciliacao() -> None:
    config = RiskConfig(allowed_assets=frozenset({"BNB"}), blocked_assets=frozenset({"BNB"}))

    assert "BNB" in ignored_assets_for(config)
