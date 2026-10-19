"""Reverificacao de QA: EmergencyExit por caminho legitimo, conciliacao em carga aleatoria e determinismo."""

import random
from decimal import Decimal

import pytest

from engine.application.backtest import (
    BacktestEngine,
    BacktestResult,
    DecisionRecord,
    HypotheticalOutcome,
    HypotheticalStatus,
    default_rules,
    simulate_signal,
    summarize_avoided,
)
from engine.application.backtest.models import DecisionAction
from engine.domain.candle import Candle
from engine.domain.signal import Signal
from engine.domain.trade import ExitReason
from engine.domain.types import Timeframe
from tests.unit.backtest.engine_helpers import (
    ENTRY_CANDLE,
    FIXED_NOW,
    QUIET,
    T0,
    Scripted,
    flat_then,
    mk,
    run,
    settings,
)

D = Decimal


async def test_Dado_GapAbaixoDoStop_Quando_LimiteExecuta_Entao_StopDoOcoAbsorveSemEmergencia() -> None:
    gap_down = ("90", "92", "89", "91")

    result = await run(flat_then([gap_down, ("91", "92", "90", "91"), ("91", "92", "90", "91")]))

    assert [d.code for d in result.decisions] == ["approved"]
    [trade] = result.trades
    assert trade.exit_reason is ExitReason.STOP_LOSS
    assert trade.exit_price < D(90)
    assert result.final_realized_equity == D(10_000) + trade.pnl


def _random_walk(seed: int, size: int) -> list[Candle]:
    rng = random.Random(seed)  # noqa: S311
    price = D(100)
    candles = [mk(i, "100", "100.2", "99.8", "100") for i in range(30)]
    for i in range(30, 30 + size):
        gap = D(
            str(round(rng.uniform(-0.04, 0.04) if rng.random() < 0.08 else rng.uniform(-0.003, 0.003), 5))
        )
        open_ = price * (1 + gap)
        close = open_ * (1 + D(str(round(rng.uniform(-0.012, 0.012), 5))))
        high = max(open_, close) * (1 + D(str(round(rng.uniform(0, 0.01), 5))))
        low = min(open_, close) * (1 - D(str(round(rng.uniform(0, 0.01), 5))))
        candles.append(mk(i, f"{open_:.4f}", f"{high:.4f}", f"{low:.4f}", f"{close:.4f}"))
        price = D(f"{close:.4f}")
    return candles


def _levels(candle: Candle) -> tuple[str, str, str]:
    entry = candle.close * D("0.999")
    return f"{entry:.4f}", f"{entry * D('0.99'):.4f}", f"{entry * D('1.04'):.4f}"


async def _run(seed: int, latency: int) -> BacktestResult:
    candles = _random_walk(seed, 400)
    strategy = Scripted(set(range(31, 430, 3)), _levels)
    engine = BacktestEngine(
        [strategy], settings(latency_candles=latency, history_window=60), clock=lambda: FIXED_NOW
    )
    return await engine.run(candles)


@pytest.mark.parametrize("latency", [1, 2, 3])
@pytest.mark.parametrize("seed", range(40))
async def test_Dado_MercadoAleatorioComGaps_Quando_Rodar_Entao_ConciliaSemTradeDuplicadoOuPerdido(
    seed: int, latency: int
) -> None:
    result = await _run(seed, latency)

    ids = [t.trade_id for t in result.trades]
    assert len(ids) == len(set(ids))
    assert result.final_realized_equity == D(10_000) + sum((t.pnl for t in result.trades), D(0))
    assert sum((r.pnl for r in result.round_trips), D(0)) == sum((t.pnl for t in result.trades), D(0))
    approved = [d for d in result.decisions if d.code == "approved"]
    expired = [d for d in result.decisions if d.code == "entry_expired"]
    settled = len(result.round_trips) + len(expired) + result.open_at_end
    assert 0 <= len(approved) - settled <= 1  # no maximo uma ordem ainda pendente no fim dos dados
    assert all(d.action is DecisionAction.SKIPPED for d in expired)


async def test_Dado_MesmaEntrada_Quando_RodarDuasVezes_Entao_ResultadoIdentico() -> None:
    first = await _run(7, 1)
    second = await _run(7, 1)

    assert first.model_dump() == second.model_dump()


async def test_Dado_Latencia2_Quando_AlvoNaVelaSeguinteAEntrada_Entao_NaoDeveAbortarComInvariante() -> None:
    target_hit = ("100.5", "112", "100.2", "111.5")

    result = await run(flat_then([QUIET, ENTRY_CANDLE, target_hit, QUIET, QUIET]), latency_candles=2)

    assert (
        result.trades == ()
    )  # o limit da parcial ainda nao estava ativo na vela do alvo; o stop seguiu ativo
    assert result.open_at_end == 1


def _after_entry(latency: int, *rows: tuple[str, str, str, str]) -> list[tuple[str, str, str, str]]:
    return [*[QUIET] * (latency - 1), ENTRY_CANDLE, *rows, QUIET, QUIET, QUIET]


@pytest.mark.parametrize("latency", [2, 3])
@pytest.mark.parametrize(
    "scenario",
    [
        [("100.5", "112", "100.2", "111.5")],
        [("100.5", "101", "94", "95")],
        [("90", "91", "89", "90")],
        [("100.5", "103", "100.2", "102.5"), ("102.5", "112", "102", "111.5")],
        [QUIET, ("100", "112", "99.9", "111.5"), ("111", "111.5", "94", "95")],
    ],
    ids=["alvo_logo_apos_entrada", "stop", "gap_abaixo_do_stop", "alvo_depois", "alvo_e_stop"],
)
async def test_Dado_LatenciaMaiorQue1_Quando_AlvoStopOuGap_Entao_ConciliaSemInvariante(
    latency: int, scenario: list[tuple[str, str, str, str]]
) -> None:
    result = await run(
        flat_then(_after_entry(latency, *scenario)), latency_candles=latency, max_signal_age_timeframes=5
    )

    assert result.final_realized_equity == D(10_000) + sum((t.pnl for t in result.trades), D(0))


@pytest.mark.parametrize("latency", [2, 3])
async def test_Dado_LatenciaMaiorQue1_Quando_StopAntesDaParcialAtiva_Entao_StopExecutaSemParcial(
    latency: int,
) -> None:
    result = await run(
        flat_then(_after_entry(latency, ("100.5", "101", "94", "95"))),
        latency_candles=latency,
        max_signal_age_timeframes=5,
    )

    [trade] = result.trades
    assert trade.exit_reason is ExitReason.STOP_LOSS
    assert trade.pnl < 0


@pytest.mark.parametrize("latency", [2, 3])
async def test_Dado_LatenciaMaiorQue1_Quando_AlvoNaVelaDaParcialAtiva_Entao_ParcialExecutaERestoSai(
    latency: int,
) -> None:
    target_hit = ("100.5", "112", "100.2", "111.5")
    rows = _after_entry(latency, *[QUIET] * (latency - 1), target_hit)

    result = await run(flat_then(rows), latency_candles=latency, max_signal_age_timeframes=5)

    assert ExitReason.TAKE_PROFIT in {t.exit_reason for t in result.trades}
    assert sum((t.pnl for t in result.trades), D(0)) > 0


@pytest.mark.parametrize("latency", [2, 3])
async def test_Dado_LatenciaMaiorQue1_Quando_AlvoAntesDaParcialAtiva_Entao_PosicaoSegueAbertaComStopAtivo(
    latency: int,
) -> None:
    result = await run(
        flat_then(_after_entry(latency, ("100.5", "112", "100.2", "111.5"))),
        latency_candles=latency,
        max_signal_age_timeframes=5,
    )

    assert result.trades == ()
    assert result.open_at_end == 1


def _signal() -> Signal:
    return Signal(
        symbol="BTC/USDT",
        timeframe=Timeframe.H1,
        created_at=T0,
        strategy="t",
        entry=D(100),
        stop_loss=D(95),
        take_profit=D(111),
    )


@pytest.mark.parametrize(
    ("touch_at", "expected"), [(1, HypotheticalStatus.STOP), (2, HypotheticalStatus.STOP)]
)
def test_Dado_SinalVetado_Quando_LimiteTocadoDentroDaValidade_Entao_SimulaEntrada(
    touch_at: int, expected: HypotheticalStatus
) -> None:
    rows = [QUIET] * 3
    rows[touch_at - 1] = ("100", "100.5", "94", "94.5")
    candles = flat_then(rows)

    outcome = simulate_signal(
        _signal(), candles, 29, equity=D(10_000), settings=settings(), rules=default_rules("BTC/USDT")
    )

    assert outcome.status is expected


def test_Dado_SinalVetado_Quando_LimiteSoTocadoAposAValidade_Entao_NaoPreenche() -> None:
    above = ("101", "102", "100.5", "101")
    candles = flat_then([above, above, ("100", "100.5", "94", "94.5"), QUIET])

    outcome = simulate_signal(
        _signal(), candles, 29, equity=D(10_000), settings=settings(), rules=default_rules("BTC/USDT")
    )

    assert outcome.status is HypotheticalStatus.NOT_FILLED


@pytest.mark.parametrize(
    "code", ["position_already_open", "entry_pending", "max_positions", "adding_to_loser"]
)
def test_Dado_VetoDeCapacidade_Quando_Resumir_Entao_NaoEntraNosFiltros(code: str) -> None:
    decision = DecisionRecord(
        at=T0,
        strategy="t",
        symbol="BTC/USDT",
        action=DecisionAction.SKIPPED,
        code=code,
        message="m",
        hypothetical=HypotheticalOutcome(
            status=HypotheticalStatus.TARGET, pnl=D(10), r_multiple=D(0), candles=1
        ),
    )

    summary = summarize_avoided([decision])

    assert (summary.capacity_net_effect, summary.risk_vetoes_net_effect) == (D(-10), D(0))
