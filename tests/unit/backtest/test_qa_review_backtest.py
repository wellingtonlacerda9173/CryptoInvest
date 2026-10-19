"""Regressoes dos achados da revisao de QA do backtest (janela de validade, dia UTC, teto de posicao)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from engine.application.backtest import HypotheticalStatus, default_rules, simulate_signal
from engine.application.backtest.models import BacktestResult
from engine.application.risk import AuditAction
from engine.application.risk.sizing import floor_to_step
from engine.domain.signal import Signal
from engine.domain.types import Timeframe
from tests.unit.backtest.engine_helpers import ENTRY_CANDLE, QUIET, T0, flat_then, run, settings

D = Decimal
RULES = default_rules("BTC/USDT")


def codes(result: BacktestResult) -> list[str]:
    return [d.code for d in result.decisions]


class TestApprovalValidityWindow:
    async def test_Dado_LimiteAprovado_Quando_TocadoSoNaTerceiraVelaAposDecisao_Entao_ExpiraSemTrade(
        self,
    ) -> None:
        above = ("101", "102", "100.5", "101")
        dip = ("101", "102", "99.5", "100.5")

        result = await run(flat_then([above, above, dip, QUIET, QUIET]))

        assert "emergency_exit" not in codes(result)
        assert result.trades == ()

    async def test_Dado_LimiteAprovado_Quando_TocadoNaSegundaVelaAposDecisao_Entao_EntraNormalmente(
        self,
    ) -> None:
        above = ("101", "102", "100.5", "101")

        result = await run(flat_then([above, ENTRY_CANDLE, QUIET, QUIET]))

        assert "emergency_exit" not in codes(result)
        assert codes(result) == ["approved"]
        assert len(result.round_trips) + result.open_at_end == 1


class TestDayBoundaryAttribution:
    async def test_Dado_StopNaUltimaVelaDoDia_Quando_NovoSinalNoDiaSeguinte_Entao_PausaDiariaJaExpirou(
        self,
    ) -> None:
        last_candle_of_day = 47  # T0 + 47h = 23:00 do dia 2
        rows = {
            16: ENTRY_CANDLE,  # indice 46 (22:00-23:00): preenche o limite em 100
            17: ("100.5", "101", "94", "94.5"),  # indice 47 (23:00-24:00): stop em 95
        }
        sequence = [rows.get(j, QUIET) for j in range(0, 30)]
        candles = flat_then(sequence)
        first_signal, second_signal = 45, 58

        result = await run(
            candles, at={first_signal, second_signal}, risk_per_trade=D("0.01"), max_daily_loss=D("0.01")
        )

        assert T0 + timedelta(hours=last_candle_of_day + 1) == datetime(2026, 1, 3, tzinfo=UTC)
        assert AuditAction.DAILY_LOSS_PAUSE in {e.action for e in result.risk_events}
        pause = next(e for e in result.risk_events if e.action is AuditAction.DAILY_LOSS_PAUSE)
        assert "2026-01-03T00:00:00" in pause.detail
        assert codes(result) == ["approved", "approved"]


class TestHypotheticalSizing:
    def test_Dado_StopCurto_Quando_Simular_Entao_TamanhoRespeitaTetoDePosicao(self) -> None:
        cfg = settings()
        signal = Signal(
            symbol="BTC/USDT",
            timeframe=Timeframe.H1,
            created_at=T0,
            strategy="t",
            entry=D(100),
            stop_loss=D(99),
            take_profit=D(103),
        )
        candles = flat_then([("100", "101", "99.5", "100.5"), ("100", "100.2", "98", "98.5"), QUIET])

        outcome = simulate_signal(
            signal, candles, 29, equity=D(10_000), settings=cfg, rules=default_rules("BTC/USDT")
        )

        assert outcome.status is HypotheticalStatus.STOP
        max_units = D(10_000) * cfg.risk.max_position_fraction / signal.entry
        worst_loss_per_unit = D("1.25")  # 1 de preco + 0,0495 de slippage + 0,199 de taxas = 1,2485
        assert -outcome.pnl <= max_units * worst_loss_per_unit


class TestMutationGaps:
    async def test_Dado_PosicaoAbertaNoFim_Quando_Marcar_Entao_EquityDescontaTaxaDeSaidaEstimada(
        self,
    ) -> None:
        result = await run(flat_then([ENTRY_CANDLE, QUIET]))

        quantity = floor_to_step(D(50) / D("5.195"), D("0.00001"))
        entry_fee = quantity * 100 * D("0.001")
        estimated_exit_fee = quantity * 100 * D("0.001")
        assert result.open_at_end == 1
        assert result.round_trips == ()
        assert result.final_equity == D(10_000) - entry_fee - estimated_exit_fee
        assert result.final_realized_equity == D(10_000)

    async def test_Dado_VendaDeEmergenciaPendente_Quando_NovoSinal_Entao_NaoAbreOutraEntrada(self) -> None:
        result = await run(flat_then([QUIET] * 8), at={29, 32}, latency_candles=2, maker_fee_rate=D("0.0025"))

        assert codes(result) == ["approved", "emergency_exit", "emergency_exit_pending"]
