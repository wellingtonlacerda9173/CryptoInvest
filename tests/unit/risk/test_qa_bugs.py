"""Regressoes dos defeitos da revisao de QA da Fase 2: cada teste descreve o comportamento CORRETO."""

import contextlib
from datetime import datetime, timedelta

import pytest

from engine.application.risk import (
    AccountSnapshot,
    ApprovedEntry,
    EmergencyExit,
    MarketSnapshot,
    Rejection,
    RiskState,
    SymbolRules,
    evaluate_entry,
    manage_position,
    open_position,
    reconcile,
)
from engine.domain.market import Balance
from engine.domain.risk import RiskConfig
from engine.domain.signal import Signal
from engine.domain.trade import ExitReason
from engine.domain.types import Timeframe
from tests.unit.risk.factories import (
    EMPTY_CALENDAR,
    NOW,
    SYMBOL,
    D,
    candle,
    history,
    market,
    position,
    rules,
    signal,
    trade,
)

CFG = RiskConfig()


def approve(sig: Signal | None = None, **overrides: object) -> ApprovedEntry | Rejection:
    args: dict[str, object] = {
        "state": RiskState.initial(D(10_000), NOW),
        "config": CFG,
        "rules": rules(),
        "positions": [],
        "pending": [],
        "market": market(),
        "history": history(),
        "calendar": EMPTY_CALENDAR,
        "now": NOW,
        "client_order_id": "e-1",
    } | overrides
    return evaluate_entry(sig or signal(), **args)  # type: ignore[arg-type]


def approved() -> ApprovedEntry:
    result = approve()
    assert isinstance(result, ApprovedEntry)
    return result


def test_open_position_nao_aceita_quantidade_acima_da_aprovada() -> None:
    entry = approved()

    result = open_position(entry, opened_at=NOW, filled_quantity=entry.order.quantity * 10)

    assert isinstance(result, EmergencyExit)
    assert result.quantity == entry.order.quantity * 10
    assert "acima da aprovada" in result.reason


def test_open_position_nao_aceita_preco_de_preenchimento_acima_do_limite() -> None:
    entry = approved()

    result = open_position(entry, opened_at=NOW, fill_price=entry.order.price * D("1.05"))

    assert isinstance(result, EmergencyExit)
    assert "acima do limite" in result.reason


def test_open_position_com_preco_zero_e_recusado() -> None:
    entry = approved()

    with pytest.raises(ValueError, match=r".+"):
        open_position(entry, opened_at=NOW, fill_price=D(0))


def test_saida_com_prejuizo_nao_e_rotulada_como_protecao_de_lucro() -> None:
    pos = position()
    # vela toca 1R (breakeven no fim da vela) mas fecha abaixo da entrada, acima do stop inicial
    result = manage_position(pos, candle("100", "105.5", "96", "97"), rules(), CFG)

    fill = result.exits[0]
    assert (fill.price - pos.entry_price) * fill.quantity < 0
    assert fill.reason is not ExitReason.PROFIT_PROTECTION


def test_vela_anterior_a_abertura_e_recusada() -> None:
    pos = position()

    with pytest.raises(ValueError, match=r".+"):
        manage_position(pos, candle("100", "101", "90", "100", at=NOW - timedelta(days=3)), rules(), CFG)


def test_parcial_sempre_informa_ajuste_da_protecao() -> None:
    pos = position().model_copy(update={"breakeven_reached": True, "stop": D(100), "highest_price": D(105)})
    result = manage_position(pos, candle("105", "110", "105", "109"), rules(), RiskConfig(trailing_r=D(5)))

    assert result.exits
    assert result.position is not None
    assert result.stop_update is not None


def test_par_cotado_em_moeda_diferente_da_conta_e_rejeitado() -> None:
    sym = "ETH/BTC"
    result = approve(
        signal(symbol=sym),
        rules=SymbolRules(
            symbol=sym,
            tick_size=D("0.00001"),
            step_size=D("0.001"),
            min_quantity=D("0.001"),
            min_notional=D("0.0001"),
        ),
        market=market(symbol=sym),
        history=history(symbol=sym),
    )

    assert isinstance(result, Rejection)


def test_token_alavancado_e_rejeitado() -> None:
    sym = "BTCUP/USDT"
    result = approve(
        signal(symbol=sym),
        rules=SymbolRules(
            symbol=sym, tick_size=D("0.01"), step_size=D("0.001"), min_quantity=D("0.001"), min_notional=D(10)
        ),
        market=market(symbol=sym),
        history=history(symbol=sym),
    )

    assert isinstance(result, Rejection)


def test_mercado_abaixo_do_stop_do_sinal_e_rejeitado() -> None:
    result = approve(market=market(bid="49.99", ask="50.01"))

    assert isinstance(result, Rejection)


def test_sinal_velho_e_rejeitado() -> None:
    old = Signal(
        symbol=SYMBOL,
        timeframe=Timeframe.H1,
        created_at=NOW - timedelta(days=3),
        strategy="t",
        entry=D(100),
        stop_loss=D(95),
        take_profit=D(110),
    )

    assert isinstance(approve(old), Rejection)


def test_pausa_de_par_nao_pode_ser_apagada_por_mutacao_do_estado() -> None:
    state = RiskState.initial(D(10_000), NOW)
    state = state.record_trade(trade("-10"), NOW, CFG).record_trade(trade("-10"), NOW, CFG)
    assert state.entry_block(SYMBOL, NOW) is not None

    with contextlib.suppress(TypeError, AttributeError):
        state.pair_pause_until.clear()

    assert state.entry_block(SYMBOL, NOW) is not None


def test_datetime_naive_e_recusado_na_virada_do_dia() -> None:
    state = RiskState.initial(D(10_000), NOW)

    with pytest.raises(ValueError, match=r".+"):
        state.roll_day(datetime(2026, 3, 10, 22, 0))


def test_queda_de_capital_observada_conta_na_perda_diaria() -> None:
    state = RiskState.initial(D(10_000), NOW).observe_equity(D(9_700), NOW, CFG)

    assert state.entry_block(SYMBOL, NOW) is not None


def test_pausa_de_par_abaixo_de_24h_e_recusada() -> None:
    with pytest.raises(ValueError, match=r".+"):
        RiskConfig(pair_pause_hours=1)


def test_poeira_de_ativo_nao_rastreado_nao_e_divergencia() -> None:
    snapshot = AccountSnapshot(
        balances={
            "ETH": Balance(asset="ETH", free=D("0.00000001"), locked=D(0)),
            "USDT": Balance(asset="USDT", free=D(500), locked=D(0)),
        },
        prices={"ETH": D(3000)},
    )

    assert reconcile([], snapshot) == ()


def test_reconciliacao_exige_stop_cobrindo_a_quantidade_da_posicao() -> None:
    # a API so recebe nomes de simbolos: nao ha como informar que o stop cobre 1% da posicao
    assert "stop_quantity" in AccountSnapshot.model_fields or "stops" in AccountSnapshot.model_fields


def test_snapshot_de_mercado_tem_horario_para_detectar_cotacao_velha() -> None:
    assert "at" in MarketSnapshot.model_fields or "captured_at" in MarketSnapshot.model_fields
