from typing import Any

from engine.application.strategies import (
    ElephantBarBreakout,
    ElephantBarBreakoutConfig,
    EmaCrossTrend,
    EmaCrossTrendConfig,
    LevelsConfig,
    StopMode,
    Strategy,
)
from engine.application.trading_config.document import StrategiesConfig


def build_strategies_from_config(config: StrategiesConfig) -> list[Strategy]:
    """Estrategias do documento versionado (so as ligadas), com todos os parametros editaveis."""
    strategies: list[Strategy] = []
    if config.elephant_enabled:
        strategies.append(
            ElephantBarBreakout(
                ElephantBarBreakoutConfig(
                    elephant=config.elephant.detector,
                    levels=LevelsConfig(reward_risk=config.reward_risk),
                    rsi_min=config.elephant.rsi_min,
                    rsi_max=config.elephant.rsi_max,
                    strong_volume_multiple=config.elephant.strong_volume_multiple,
                    min_confidence=config.min_confidence,
                )
            )
        )
    if config.ema_enabled:
        ema = config.ema
        strategies.append(
            EmaCrossTrend(
                EmaCrossTrendConfig(
                    levels=LevelsConfig(stop_mode=StopMode.ATR, reward_risk=config.reward_risk),
                    fast_period=ema.fast_period,
                    slow_period=ema.slow_period,
                    trend_period=ema.trend_period,
                    adx_min=ema.adx_min,
                    rsi_range=(ema.rsi_min, ema.rsi_max),
                    min_confidence=config.min_confidence,
                )
            )
        )
    return strategies


def strategy_settings(config: StrategiesConfig) -> dict[str, Any]:
    return config.model_dump(mode="json")
