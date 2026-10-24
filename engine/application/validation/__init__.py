from engine.application.validation.collect import collect_runs, collect_samples, samples_from_result
from engine.application.validation.models import (
    BuyHold,
    EntryReport,
    Judgement,
    RandomReference,
    TradeStats,
    ValidationConfig,
    ValidationReport,
    Verdict,
    WindowReport,
)
from engine.application.validation.references import buy_and_hold, random_reference, trade_stats
from engine.application.validation.report import render_validation_markdown, write_validation_report
from engine.application.validation.verdict import (
    beats_buy_hold,
    corrected_alpha,
    judge,
    min_attainable_p,
    required_simulations,
)
from engine.application.validation.walk_forward import (
    NotEnoughHistoryError,
    run_validation,
    selector_walk_forward,
    validate_samples,
    window_bounds,
)

__all__ = [
    "BuyHold",
    "EntryReport",
    "Judgement",
    "NotEnoughHistoryError",
    "RandomReference",
    "TradeStats",
    "ValidationConfig",
    "ValidationReport",
    "Verdict",
    "WindowReport",
    "beats_buy_hold",
    "buy_and_hold",
    "collect_runs",
    "collect_samples",
    "corrected_alpha",
    "judge",
    "min_attainable_p",
    "random_reference",
    "render_validation_markdown",
    "required_simulations",
    "run_validation",
    "samples_from_result",
    "selector_walk_forward",
    "trade_stats",
    "validate_samples",
    "window_bounds",
    "write_validation_report",
]
