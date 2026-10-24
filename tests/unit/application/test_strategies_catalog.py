from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from engine.application.indicators import CandleArrays, atr
from engine.application.risk import ApprovedEntry
from engine.application.risk.sizing import net_reward_risk_ok
from engine.application.strategies import STRATEGY_REGISTRY, Strategy, StrategyContext
from engine.application.strategies._support import build_signal
from engine.application.strategies.confluence import CONFLUENCE_FAMILIES, TRIGGERS, independent_families
from engine.application.strategies.levels import LevelsConfig
from engine.domain.candle import Candle
from engine.domain.risk import RiskConfig
from tests.unit.application.helpers import build_candles, candle_row, wave_rows
from tests.unit.application.strategy_series import CASES, CTX, FEE, Case
from tests.unit.application.test_strategies import assert_valid, future_candles, remove_candle
from tests.unit.risk.test_strategy_gate_integration import _approve

IDS = [case.id for case in CASES]
CASE_PARAMS = pytest.param
EVERY_CASE = pytest.mark.parametrize("case", CASES, ids=IDS)


def vetoes() -> list[Any]:
    return [pytest.param(case, label, id=f"{case.id}-{label}") for case in CASES for label in case.vetoes]


@EVERY_CASE
class TestEveryStrategy:
    def test_positive_scenario(self, case: Case) -> None:
        candles = case.candles()
        strategy = case.build()
        assert isinstance(strategy, Strategy)

        result = assert_valid(strategy.evaluate(candles, case.context))

        sig = result.signal
        assert sig.strategy == strategy.name == case.id
        assert sig.entry == candles[-1].close
        assert sig.created_at == candles[-1].close_time
        assert sig.stop_loss < candles[-1].close
        assert result.triggers == case.triggers
        assert set(result.triggers) <= TRIGGERS
        assert not set(result.triggers) & set(CONFLUENCE_FAMILIES)

    def test_net_reward_risk_meets_the_gate_minimum(self, case: Case) -> None:
        result = assert_valid(case.build().evaluate(case.candles(), case.context))
        sig = result.signal
        assert net_reward_risk_ok(sig.entry, sig.stop_loss, sig.take_profit, FEE, Decimal(2))

    def test_signal_exists_only_on_the_trigger_candle(self, case: Case) -> None:
        assert case.build().evaluate(case.candles()[:-1], case.context) is None

    def test_open_last_candle_is_rejected(self, case: Case) -> None:
        candles = case.candles()
        candles[-1] = candles[-1].model_copy(update={"closed": False})
        assert case.build().evaluate(candles, case.context) is None

    def test_open_candle_in_history_is_rejected(self, case: Case) -> None:
        candles = case.candles()
        candles[-5] = candles[-5].model_copy(update={"closed": False})
        assert case.build().evaluate(candles, case.context) is None

    def test_gap_in_history_is_rejected(self, case: Case) -> None:
        candles = remove_candle(case.candles(), len(case.candles()) - 10)
        assert case.build().evaluate(candles, case.context) is None

    def test_short_history_is_rejected(self, case: Case) -> None:
        assert case.build().evaluate(case.candles()[-20:], case.context) is None
        assert case.build().evaluate([], case.context) is None

    def test_future_candles_do_not_change_past_decision(self, case: Case) -> None:
        candles = case.candles()
        extended = [*candles, *future_candles(candles[-1], 4)]
        strategy = case.build()
        for cut in range(len(candles) - 6, len(candles) + 1):
            assert strategy.evaluate(candles[:cut], case.context) == strategy.evaluate(
                extended[:cut], case.context
            )

    def test_min_confluences_above_available_families_blocks_signal(self, case: Case) -> None:
        assert case.build(min_confluences=5).evaluate(case.candles(), case.context) is None

    def test_min_confidence_blocks_signal(self, case: Case) -> None:
        assert case.build(min_confidence=1.0).evaluate(case.candles(), case.context) is None

    def test_config_is_frozen_and_rejects_fewer_than_three_confluences(self, case: Case) -> None:
        strategy = case.build()
        with pytest.raises(ValidationError):
            strategy.config.min_confluences = 1  # type: ignore[attr-defined]
        with pytest.raises(ValidationError):
            case.build(min_confluences=2)

    def test_stop_is_mandatory_below_entry_and_target_above(self, case: Case) -> None:
        sig = assert_valid(case.build().evaluate(case.candles(), case.context)).signal
        assert 0 < sig.stop_loss < sig.entry < sig.take_profit

    def test_stop_sits_beyond_the_setup_structure(self, case: Case) -> None:
        candles = case.candles()
        sig = assert_valid(case.build().evaluate(candles, case.context)).signal
        if case.stop_floor is not None:
            assert sig.stop_loss < case.stop_floor(candles)
        if case.atr_stop_multiple is not None:
            data = CandleArrays.from_candles(candles)
            atr_value = float(atr(data.high, data.low, data.close, 14)[-1])
            assert float(sig.entry - sig.stop_loss) == pytest.approx(case.atr_stop_multiple * atr_value)

    def test_taker_fee_in_context_raises_the_target(self, case: Case) -> None:
        cheap = StrategyContext(htf_trend=case.context.htf_trend, taker_fee_rate=Decimal(0))
        costly = StrategyContext(htf_trend=case.context.htf_trend, taker_fee_rate=Decimal("0.005"))
        low = case.build().evaluate(case.candles(), cheap)
        high = case.build().evaluate(case.candles(), costly)
        assert low is not None
        assert high is None or high.signal.take_profit > low.signal.take_profit

    def test_signal_is_approved_by_the_real_gate(self, case: Case) -> None:
        approved = _approve(case.candles(), case.build(), case.context)
        assert isinstance(approved, ApprovedEntry)
        order = approved.order
        assert net_reward_risk_ok(
            order.price, order.stop_loss, order.take_profit, RiskConfig().taker_fee_rate, Decimal(2)
        )
        assert approved.signal.strategy == case.id


@pytest.mark.parametrize(("case", "label"), vetoes())
def test_vetoes_and_missing_triggers_block_the_signal(case: Case, label: str) -> None:
    rows, context, config = case.vetoes[label]()
    candles = build_candles(rows)
    if config:
        baseline = case.build().evaluate(candles, context)
        assert baseline is not None, "o cenario base deve disparar: o veto e a unica causa"
    assert case.build(**config).evaluate(candles, context) is None


class TestSharedGuards:
    def test_registry_has_all_strategies_and_matching_names(self) -> None:
        expected = {case.id for case in CASES} | {"elephant_bar_breakout", "ema_cross_trend"}
        assert set(STRATEGY_REGISTRY) == expected
        for name, factory in STRATEGY_REGISTRY.items():
            strategy = factory()
            assert isinstance(strategy, Strategy)
            assert strategy.name == name

    def test_registry_is_read_only(self) -> None:
        with pytest.raises(TypeError):
            STRATEGY_REGISTRY["x"] = lambda: None  # type: ignore[index]

    def test_build_signal_counts_families_not_checks(self) -> None:
        candles = _plain_candles()
        correlated = {
            "preco_acima_da_ema_de_tendencia": True,
            "dominio_comprador_di": True,
            "tendencia_superior_de_alta": True,
            "volume_acima_da_media": True,
        }
        assert self._build(candles, correlated) is None

    def test_build_signal_accepts_three_independent_families(self) -> None:
        independent = {"volume_acima_da_media": True, "rsi_em_zona_de_forca": True, "sem_exaustao": True}
        result = self._build(_plain_candles(), independent)
        assert result is not None
        assert len(independent_families(result.confluences)) == 3

    def test_build_signal_rejects_target_beyond_ceiling(self) -> None:
        independent = {"volume_acima_da_media": True, "rsi_em_zona_de_forca": True, "sem_exaustao": True}
        assert self._build(_plain_candles(), independent, max_target=Decimal("100.5")) is None

    def test_build_signal_rejects_stop_outside_atr_band(self) -> None:
        independent = {"volume_acima_da_media": True, "rsi_em_zona_de_forca": True, "sem_exaustao": True}
        far = Decimal(80)
        assert self._build(_plain_candles(), independent, stop=far) is None

    @staticmethod
    def _build(
        candles: list[Candle],
        checks: dict[str, bool],
        max_target: Decimal | None = None,
        stop: Decimal | None = None,
    ) -> Any:
        from engine.application.strategies._support import CommonConfig

        return build_signal(
            name="x",
            candles=candles,
            cfg=CommonConfig(levels=LevelsConfig()),
            context=CTX,
            atr_value=1.0,
            structure_stop=stop if stop is not None else candles[-1].close - Decimal("0.5"),
            checks=checks,
            triggers=("t",),
            summary="s",
            max_target=max_target,
        )


def _plain_candles() -> list[Candle]:
    rows, _ = wave_rows(5)
    return build_candles([*rows, candle_row(100.0, 100.0)])
