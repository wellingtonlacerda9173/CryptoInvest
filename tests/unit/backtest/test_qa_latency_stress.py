"""QA: latencia 1-3 sob carga aleatoria (cenarios de alvo e reversao na janela) sem lookahead e conciliado."""

from decimal import Decimal

import pytest

from tests.unit.backtest.engine_helpers import ENTRY_CANDLE, QUIET, flat_then, run
from tests.unit.backtest.test_reverification_qa import _run

D = Decimal


@pytest.mark.parametrize("latency", [1, 2, 3])
@pytest.mark.parametrize("seed", range(40, 120))
async def test_Dado_CargaAleatoria_Quando_Latencia123_Entao_ConciliaEPerdaNoStopLimitada(
    seed: int, latency: int
) -> None:
    result = await _run(seed, latency)

    assert result.final_realized_equity == D(10_000) + sum((t.pnl for t in result.trades), D(0))
    assert sum((r.pnl for r in result.round_trips), D(0)) == sum((t.pnl for t in result.trades), D(0))
    for rt in result.round_trips:
        assert rt.r_multiple >= D("-6")  # gap 4% + pavio 1% sobre stop 1%


@pytest.mark.parametrize("latency", [2, 3])
async def test_Dado_AlvoNaJanelaESeguidoDeReversao_Quando_GestaoAtiva_Entao_SaiNoStopSemLucroFantasma(
    latency: int,
) -> None:
    rows = [*[QUIET] * (latency - 1), ENTRY_CANDLE, ("100.5", "112", "100.2", "111.5")]
    rows += [("111", "111.5", "94", "95")] * 2 + [QUIET] * 3

    result = await run(flat_then(rows), latency_candles=latency, max_signal_age_timeframes=5)

    assert sum((t.pnl for t in result.trades), D(0)) <= 0
    assert result.final_realized_equity == D(10_000) + sum((t.pnl for t in result.trades), D(0))
