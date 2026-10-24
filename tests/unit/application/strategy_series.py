"""Series sinteticas deterministicas: cada caso termina EXATAMENTE na vela que dispara a estrategia."""

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from engine.application.strategies import (
    BollingerRsiConfig,
    BollingerRsiReversion,
    BullishEngulfing,
    BullishEngulfingConfig,
    BullishPinBar,
    BullishPinBarConfig,
    DonchianBreakout,
    DonchianBreakoutConfig,
    MacdCross,
    MacdCrossConfig,
    RangeBreakout,
    RangeBreakoutConfig,
    RsiBullishDivergence,
    RsiDivergenceConfig,
    Strategy,
    StrategyContext,
    SupertrendFlip,
    SupertrendFlipConfig,
    SupportRetest,
    SupportRetestConfig,
    VwapConfig,
    VwapReclaim,
)
from engine.domain.candle import Candle
from engine.domain.risk import RiskConfig
from tests.unit.application.helpers import Row, build_candles, candle_row, reversal_rows, wave_rows

FEE = RiskConfig().taker_fee_rate
CTX = StrategyContext(taker_fee_rate=FEE)
CTX_UP = StrategyContext(taker_fee_rate=FEE, htf_trend="up")
CTX_DOWN = StrategyContext(taker_fee_rate=FEE, htf_trend="down")


def _decline(price: float, target: float, count: int, volume: float = 100.0) -> tuple[list[Row], float]:
    step = (price - target) / count
    rows: list[Row] = []
    for _ in range(count):
        rows.append(candle_row(price, price - step, volume, 0.1))
        price -= step
    return rows, price


def _wave_then_decline(
    base: int, peak: float, target: float, count: int = 7
) -> tuple[list[Row], float, float]:
    rows, price = wave_rows(base)
    rows.append(candle_row(price, peak, 100.0, 0.1))
    down, end = _decline(peak, target, count)
    return [*rows, *down], end, down[-1][0]


def engulfing_rows(*, volume: float = 220.0, peak: float = 101.0, target: float = 98.4) -> list[Row]:
    rows, end, prev_open = _wave_then_decline(60, peak, target)
    return [*rows, candle_row(end, prev_open + 0.1, volume, 0.05)]


def pin_bar_rows(*, upper_wick: float = 0.02, volume: float = 220.0) -> list[Row]:
    rows, _, _ = _wave_then_decline(60, 101.0, 98.4)
    return [*rows, (98.45, 98.57 + upper_wick - 0.02, 97.9, 98.55, volume)]


def retest_rows(*, low: float = 98.38, volume: float = 150.0, bullish: bool = True) -> list[Row]:
    rows, price = wave_rows(110)
    down, _ = _decline(price, 99.0, 3) if price > 99.0 else ([], price)
    close = 99.1 if bullish else 98.5
    return [*rows, *down, (max(98.8, low + 0.05), 99.15, low, close, volume)]


def pullback_rows(cut: int, *, last_volume: float | None = None) -> list[Row]:
    """Tendencia de alta (60), correcao de 10 velas e retomada; supertrend vira em 74 e o MACD cruza em 76."""
    rows: list[Row] = []
    price = 100.0
    for i in range(60):
        rows.append(candle_row(price, price + 0.3 + 0.1 * math.sin(i), 100 + (i % 3) * 5, 0.2))
        price = rows[-1][3]
    for _ in range(10):
        rows.append(candle_row(price, price - 0.5, 90, 0.2))
        price -= 0.5
    for i in range(15):
        rows.append(candle_row(price, price + 0.8 + 0.05 * math.sin(i), 160 + (i % 3) * 5, 0.2))
        price = rows[-1][3]
    rows = rows[:cut]
    if last_volume is not None:
        o, h, low, c, _ = rows[-1]
        rows[-1] = (o, h, low, c, last_volume)
    return rows


def range_breakout_rows(*, volume: float = 300.0, close: float = 102.5) -> list[Row]:
    rows, _ = wave_rows(70)
    return [*rows, candle_row(100.0, close, volume, 0.1)]


def bollinger_rows(*, drop: float = 2.0, recovery: float = 1.5) -> list[Row]:
    rows, price = wave_rows(60)
    rows.append(candle_row(price, price - drop, 110, 0.1))
    price -= drop
    return [*rows, candle_row(price, price + recovery, 150, 0.05)]


def divergence_rows(*, extra_bar: bool = False) -> list[Row]:
    rows, price = wave_rows(40)
    for _ in range(6):
        rows.append(candle_row(price, price - 1.0, 100, 0.1))
        price -= 1.0
    for _ in range(5):
        rows.append(candle_row(price, price + 0.6, 100, 0.1))
        price += 0.6
    for _ in range(7):
        rows.append(candle_row(price, price - 0.45, 100, 0.1))
        price -= 0.45
    rows.append((price, price + 0.1, price - 0.5, price + 0.05, 100.0))
    price += 0.05
    for i, step in enumerate((0.25, 0.3, 0.45)):
        rows.append(candle_row(price, price + step, 200 if i == 2 else 100, 0.05))
        price += step
    if extra_bar:
        rows.append(candle_row(price, price + 0.3, 200, 0.05))
    return rows


CONFIG_CLASSES: dict[type[Any], type[Any]] = {
    BullishEngulfing: BullishEngulfingConfig,
    BullishPinBar: BullishPinBarConfig,
    SupportRetest: SupportRetestConfig,
    SupertrendFlip: SupertrendFlipConfig,
    DonchianBreakout: DonchianBreakoutConfig,
    RangeBreakout: RangeBreakoutConfig,
    BollingerRsiReversion: BollingerRsiConfig,
    MacdCross: MacdCrossConfig,
    RsiBullishDivergence: RsiDivergenceConfig,
    VwapReclaim: VwapConfig,
}


@dataclass(frozen=True)
class Case:
    id: str
    strategy_cls: type[Any]
    rows: Callable[[], list[Row]]
    context: StrategyContext
    triggers: tuple[str, ...]
    stop_floor: Callable[[list[Candle]], Decimal] | None = None
    atr_stop_multiple: float | None = None
    vetoes: dict[str, Callable[[], tuple[list[Row], StrategyContext, dict[str, Any]]]] = field(
        default_factory=dict
    )

    def candles(self) -> list[Candle]:
        return build_candles(self.rows())

    def build(self, **config: Any) -> Strategy:
        strategy: Strategy = (
            self.strategy_cls(CONFIG_CLASSES[self.strategy_cls](**config)) if config else self.strategy_cls()
        )
        return strategy


def _veto(
    rows: Callable[[], list[Row]], ctx: StrategyContext = CTX, **config: Any
) -> Callable[[], tuple[list[Row], StrategyContext, dict[str, Any]]]:
    return lambda: (rows(), ctx, config)


def _last_low(candles: list[Candle]) -> Decimal:
    return candles[-1].low


def _low2(candles: list[Candle]) -> Decimal:
    return min(c.low for c in candles[-2:])


def _reversal(cut: int = 54) -> list[Row]:
    return reversal_rows()[:cut]


CASES: tuple[Case, ...] = (
    Case(
        "bullish_engulfing",
        BullishEngulfing,
        engulfing_rows,
        CTX,
        ("engolfo_de_alta", "queda_previa"),
        stop_floor=_low2,
        vetoes={
            "htf_de_baixa": _veto(engulfing_rows, CTX_DOWN),
            "sem_queda_previa": _veto(lambda: engulfing_rows(peak=99.0, target=98.4)),
            "sem_engolfo": _veto(lambda: [*engulfing_rows()[:-1], candle_row(98.4, 98.45, 220.0, 0.05)]),
            "vela_vendedora": _veto(lambda: [*engulfing_rows()[:-1], candle_row(98.9, 98.4, 220.0, 0.05)]),
        },
    ),
    Case(
        "bullish_pin_bar",
        BullishPinBar,
        pin_bar_rows,
        CTX_UP,
        ("pin_bar_de_alta", "queda_previa"),
        stop_floor=_low2,
        vetoes={
            "htf_de_baixa": _veto(pin_bar_rows, CTX_DOWN),
            "pavio_superior_longo": _veto(lambda: pin_bar_rows(upper_wick=0.3), CTX_UP),
            "familias_insuficientes": _veto(lambda: pin_bar_rows(volume=100.0), CTX),
        },
    ),
    Case(
        "support_retest",
        SupportRetest,
        retest_rows,
        CTX_UP,
        ("reteste_de_nivel_confirmado", "rejeicao_de_alta_no_nivel"),
        stop_floor=_last_low,
        vetoes={
            "htf_de_baixa": _veto(retest_rows, CTX_DOWN),
            "longe_do_nivel": _veto(lambda: retest_rows(low=98.9), CTX_UP),
            "vela_vendedora": _veto(lambda: retest_rows(bullish=False), CTX_UP),
            "resistencia_colada": _veto(retest_rows, CTX_UP, min_room_atr=4.0),
            "nivel_sem_toques_suficientes": _veto(retest_rows, CTX_UP, min_touches=99),
        },
    ),
    Case(
        "supertrend_flip",
        SupertrendFlip,
        lambda: pullback_rows(74),
        CTX,
        ("supertrend_virou_para_alta",),
        stop_floor=_last_low,
        vetoes={
            "htf_de_baixa": _veto(lambda: pullback_rows(74), CTX_DOWN),
            "mercado_lateral_adx": _veto(lambda: pullback_rows(74), CTX, adx_min=99.0),
            "rsi_esticado": _veto(lambda: pullback_rows(74), CTX, rsi_veto=50.1),
        },
    ),
    Case(
        "donchian_breakout",
        DonchianBreakout,
        _reversal,
        CTX,
        ("rompimento_do_canal_de_donchian",),
        atr_stop_multiple=2.0,
        vetoes={
            "htf_de_baixa": _veto(_reversal, CTX_DOWN),
            "perseguicao": _veto(_reversal, CTX, max_extension_atr=0.01),
            "rsi_esticado": _veto(_reversal, CTX, rsi_veto=50.1),
            "sem_rompimento": _veto(lambda: _reversal(48)),
        },
    ),
    Case(
        "range_breakout",
        RangeBreakout,
        range_breakout_rows,
        CTX,
        ("rompimento_de_range_lateral", "range_lateral_valido", "volume_no_rompimento"),
        stop_floor=_last_low,
        vetoes={
            "htf_de_baixa": _veto(range_breakout_rows, CTX_DOWN),
            "sem_volume_no_rompimento": _veto(lambda: range_breakout_rows(volume=100.0)),
            "range_largo_demais": _veto(range_breakout_rows, CTX, max_width_atr=1.0),
            "range_sem_adx_baixo": _veto(range_breakout_rows, CTX, adx_lateral_max=1.0),
            "fechamento_dentro_do_range": _veto(lambda: range_breakout_rows(close=101.0)),
            "perseguicao": _veto(range_breakout_rows, CTX, max_extension_atr=0.1),
            "sem_estrutura_testada": _veto(range_breakout_rows, CTX, min_touches=99),
        },
    ),
    Case(
        "bollinger_rsi_reversion",
        BollingerRsiReversion,
        bollinger_rows,
        CTX,
        ("regime_lateral_adx_baixo", "retorno_para_dentro_da_banda_inferior", "rsi_sobrevendido"),
        stop_floor=_low2,
        vetoes={
            "htf_de_baixa": _veto(bollinger_rows, CTX_DOWN),
            "regime_de_tendencia": _veto(bollinger_rows, CTX, adx_max=1.0),
            "sem_sobrevenda": _veto(bollinger_rows, CTX, rsi_oversold=1.0),
            "alvo_alem_da_banda_superior": _veto(bollinger_rows, CTX, levels={"reward_risk": Decimal(10)}),
            "sem_retorno_a_banda": _veto(lambda: bollinger_rows(recovery=-0.3)),
        },
    ),
    Case(
        "macd_cross",
        MacdCross,
        lambda: pullback_rows(76),
        CTX,
        ("cruzamento_macd_para_cima", "adx_em_tendencia"),
        atr_stop_multiple=1.5,
        vetoes={
            "htf_de_baixa": _veto(lambda: pullback_rows(76), CTX_DOWN),
            "sem_tendencia_adx": _veto(lambda: pullback_rows(76), CTX, adx_min=99.0),
            "rsi_esticado": _veto(lambda: pullback_rows(76), CTX, rsi_veto=50.1),
        },
    ),
    Case(
        "rsi_bullish_divergence",
        RsiBullishDivergence,
        divergence_rows,
        CTX,
        ("divergencia_de_alta_do_rsi", "confirmacao_do_pivo_a_direita"),
        stop_floor=lambda candles: candles[-4].low,
        vetoes={
            "htf_de_baixa": _veto(divergence_rows, CTX_DOWN),
            "sem_melhora_do_rsi": _veto(divergence_rows, CTX, min_rsi_improvement=50.0),
            "pivo_ja_confirmado_ha_uma_vela": _veto(lambda: divergence_rows(extra_bar=True)),
            "pivos_muito_proximos": _veto(divergence_rows, CTX, min_pivot_gap=30, max_pivot_gap=40),
        },
    ),
    Case(
        "vwap_reclaim",
        VwapReclaim,
        _reversal,
        CTX,
        ("reclaim_do_vwap", "sessao_com_amostra_minima"),
        stop_floor=lambda candles: min(c.low for c in candles[-3:]),
        vetoes={
            "htf_de_baixa": _veto(_reversal, CTX_DOWN),
            "sessao_curta_demais": _veto(_reversal, CTX, min_session_candles=60),
            "perseguicao": _veto(_reversal, CTX, max_extension_atr=0.001),
            "rsi_esticado": _veto(_reversal, CTX, rsi_veto=50.1),
        },
    ),
)
