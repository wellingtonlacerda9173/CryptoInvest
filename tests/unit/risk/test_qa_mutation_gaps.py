"""Testes que matam mutantes sobreviventes na revisao de QA (regras criticas sem cobertura)."""

import pytest
from pydantic import ValidationError

from engine.application.risk import (
    ManagedPosition,
    RejectionCode,
    RiskState,
    SymbolRules,
    manage_position,
    size_position,
)
from engine.domain.risk import RiskConfig
from tests.unit.risk.factories import NOW, SYMBOL, D, candle, position, rules, trade

CFG = RiskConfig()


def test_trailing_com_distancia_grande_nunca_baixa_o_stop_de_breakeven() -> None:
    pos = position().model_copy(update={"breakeven_reached": True, "stop": D(100), "highest_price": D(105)})

    result = manage_position(
        pos, candle("105", "110", "105", "109"), rules(), RiskConfig(trailing_r=D(5), taker_fee_rate=D(0))
    )

    assert result.position is not None
    assert result.position.stop == D(100)


def test_empate_nao_conta_como_perda_na_sequencia() -> None:
    state = RiskState.initial(D(10_000), NOW)

    state = state.record_trade(trade("-10"), NOW, CFG).record_trade(trade("0"), NOW, CFG)

    assert state.entry_block(SYMBOL, NOW) is None
    assert state.loss_streaks[SYMBOL] == 1


def test_perda_diaria_usa_o_capital_do_inicio_do_dia_e_nao_o_atual() -> None:
    state = RiskState.initial(D(10_000), NOW).record_trade(trade("200"), NOW, CFG)

    state = state.record_trade(trade("-300"), NOW, CFG)

    assert state.day_pnl == D(-100)
    assert state.entry_block(SYMBOL, NOW).code is RejectionCode.DAILY_LOSS_PAUSE  # type: ignore[union-attr]


def test_quantidade_abaixo_do_minimo_da_corretora_e_recusada_mesmo_com_notional_ok() -> None:
    lot = SymbolRules(
        symbol=SYMBOL, tick_size=D("0.01"), step_size=D("0.001"), min_quantity=D(1), min_notional=D(0)
    )

    result = size_position(
        equity=D(10_000),
        risk_fraction=D("0.0004"),
        max_position_fraction=D("0.25"),
        entry=D(100),
        stop_loss=D(95),
        rules=lot,
        fee_rate=D(0),
    )

    assert getattr(result, "code", None) is RejectionCode.QUANTITY_BELOW_MIN


def test_stop_igual_a_entrada_e_recusado_sem_dividir_por_zero() -> None:
    result = size_position(
        equity=D(10_000),
        risk_fraction=D("0.005"),
        max_position_fraction=D("0.25"),
        entry=D(100),
        stop_loss=D(100),
        rules=rules(),
        fee_rate=D(0),
    )

    assert getattr(result, "code", None) is RejectionCode.INVALID_EQUITY


def test_parcial_e_pulada_quando_o_resto_ficaria_abaixo_do_minimo() -> None:
    lot = rules(min_quantity=D("0.1"), min_notional=D(0))
    pos = position(quantity="0.3")
    config = RiskConfig(partial_exit_fraction=D("0.9"), taker_fee_rate=D(0))

    result = manage_position(pos, candle("100", "110", "100", "109"), lot, config)

    assert result.exits == ()
    assert result.position is not None
    assert result.position.quantity == D("0.3")


def test_posicao_exige_stop_inicial_abaixo_da_entrada() -> None:
    with pytest.raises(ValidationError, match="initial_stop"):
        ManagedPosition(
            symbol=SYMBOL,
            entry_price=D(100),
            initial_quantity=D(1),
            quantity=D(1),
            initial_stop=D(100),
            stop=D(100),
            take_profit=D(110),
            highest_price=D(100),
            opened_at=NOW,
        )
