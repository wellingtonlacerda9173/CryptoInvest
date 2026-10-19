"""Reverificacao 2 da revisao de QA: bypasses novos (devem segurar) e defeitos ja corrigidos."""

import copy
import math
from datetime import timedelta
from decimal import Decimal

import pytest

from engine.application.risk import (
    ApprovedEntry,
    EmergencyExit,
    EventCalendar,
    EventWindow,
    Rejection,
    RejectionCode,
    RiskState,
    evaluate_entry,
    manage_position,
    open_position,
)
from engine.application.risk.approval import ApprovalError
from engine.application.strategies import ElephantBarBreakout, StrategyContext
from engine.application.strategies.confluence import independent_families
from engine.domain.risk import RiskConfig
from tests.unit.application.helpers import build_candles, candle_row
from tests.unit.risk.factories import (
    EMPTY_CALENDAR,
    NOW,
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


def approve(sig=None, **overrides: object) -> ApprovedEntry | Rejection:  # type: ignore[no-untyped-def]
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


def test_aprovacao_forjada_por_object_new_e_recusada_pelo_open_position() -> None:
    forged = object.__new__(ApprovedEntry)

    with pytest.raises(ApprovalError):
        open_position(forged, opened_at=NOW)


def test_aprovacao_com_ordem_adulterada_e_recusada() -> None:
    entry = approved()
    object.__setattr__(entry.order, "quantity", D(999))

    with pytest.raises(ApprovalError):
        open_position(entry, opened_at=NOW)


def test_aprovacao_nao_sobrevive_a_copia() -> None:
    with pytest.raises(AttributeError):
        copy.copy(approved())


@pytest.mark.parametrize("fee", [D("NaN"), D("Infinity"), D("-0.01")])
def test_open_position_recusa_taxa_invalida(fee: Decimal) -> None:
    with pytest.raises(ValueError, match=r".+"):
        open_position(approved(), opened_at=NOW, fee=fee)


def test_mapas_do_estado_sao_imutaveis_apos_transicao() -> None:
    state = RiskState.initial(D(10_000), NOW).record_trade(trade("-10"), NOW, CFG)

    with pytest.raises(TypeError):
        state.loss_streaks.update({"BTC/USDT": 0})  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        state.pair_pause_until["X/USDT"] = NOW  # type: ignore[index]


def test_historical_nao_dispensa_janela_de_evento() -> None:
    calendar = EventCalendar(windows=(EventWindow(start=NOW, end=NOW + timedelta(hours=1)),))

    result = approve(calendar=calendar, mode="backtest")

    assert isinstance(result, Rejection)
    assert result.code is RejectionCode.EVENT_WINDOW


def test_calendario_sem_cobertura_recusa_ao_vivo() -> None:
    result = approve(calendar=EventCalendar())

    assert isinstance(result, Rejection)
    assert result.code is RejectionCode.EVENT_CALENDAR_STALE


def test_observe_equity_com_horario_naive_e_recusado() -> None:
    state = RiskState.initial(D(10_000), NOW)

    with pytest.raises(ValueError, match=r"timezone-aware"):
        state.observe_equity(D(9_000), NOW.replace(tzinfo=None), CFG)


def test_gate_com_horario_naive_e_recusado() -> None:
    with pytest.raises(ValueError, match=r"timezone-aware"):
        approve(now=NOW.replace(tzinfo=None))


def test_gate_recusa_alvo_cujo_retorno_liquido_de_taxas_e_negativo() -> None:
    cfg = RiskConfig(taker_fee_rate=D("0.001"))
    sig = signal(entry="100", stop="99.9", target="100.2")

    result = approve(sig, config=cfg, market=market(bid="99.99", ask="100.01"))

    assert isinstance(result, Rejection)


def test_stop_update_nunca_fica_acima_da_maxima_da_vela() -> None:
    pos = position(entry="100", stop="99.9", target="100.2")

    result = manage_position(pos, candle("100", "100.1", "100.0", "100.1"), rules(), CFG)

    assert result.stop_update is None or result.stop_update <= D("100.1")


def test_open_position_recusa_taxa_absurda_frente_a_aprovada() -> None:
    result = open_position(approved(), opened_at=NOW, fee=D(100_000))

    assert isinstance(result, EmergencyExit)
    assert "taxa anormal" in result.reason


def test_open_position_recusa_aprovacao_velha() -> None:
    entry = approved()

    result = open_position(entry, opened_at=entry.order.created_at + timedelta(days=30))

    assert isinstance(result, EmergencyExit)
    assert "expirada" in result.reason


def test_elephant_breakout_em_queda_sem_confirmacao_de_tendencia_nao_gera_sinal() -> None:
    rows = []
    price = 300.0
    for i in range(60):
        nxt = price - 1.0 + 0.3 * math.sin(i * 0.9)
        rows.append(candle_row(price, nxt, 100 + (i % 5) * 2, 0.15))
        price = nxt
    for i in range(12):
        nxt = price + 0.8 * math.sin(i * 0.9)
        rows.append(candle_row(price, nxt, 100 + (i % 5) * 2, 0.15))
        price = nxt
    rows.append(candle_row(price, price + 2, 400.0, 0.1))

    result = ElephantBarBreakout().evaluate(build_candles(rows), StrategyContext())

    assert result is None


def test_calendario_carregado_em_data_futura_recusa() -> None:
    calendar = EventCalendar(
        loaded_at=NOW + timedelta(days=1), valid_until=NOW + timedelta(days=5), acknowledged_no_events=True
    )

    result = approve(calendar=calendar)

    assert isinstance(result, Rejection)
    assert result.code is RejectionCode.EVENT_CALENDAR_STALE


@pytest.mark.parametrize(
    "names",
    [
        ("preco_acima_da_ema_de_tendencia", "dominio_comprador_di", "tendencia_superior_de_alta"),
        ("volume_forte", "volume_acima_da_media", "rsi_com_espaco"),
    ],
)
def test_confluencias_correlacionadas_nao_somam_tres_familias(names: tuple[str, ...]) -> None:
    assert len(independent_families(names)) < 3
