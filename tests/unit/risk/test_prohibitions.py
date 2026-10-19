import re

import engine.application.risk as risk
from engine.application.risk import ManagedPosition, RiskState
from engine.domain.order import EntryOrder

FORBIDDEN_NAMES = re.compile(
    r"lower|widen|remove_stop|cancel_stop|disable|martingale|leverage|margin|hold", re.I
)


def test_public_api_exposes_no_way_to_lower_or_drop_a_stop() -> None:
    public = [*risk.__all__, *(n for n in dir(RiskState) if not n.startswith("_"))]

    assert not [name for name in public if FORBIDDEN_NAMES.search(name)]


def test_models_have_no_leverage_margin_or_short_fields() -> None:
    fields = set(ManagedPosition.model_fields) | set(EntryOrder.model_fields)

    assert not [f for f in fields if re.search(r"leverage|margin|short|futures", f)]


def test_only_reactivate_clears_a_halt() -> None:
    clearing = [
        name
        for name in dir(RiskState)
        if not name.startswith("_")
        and name in {"reactivate", "resume", "pause", "roll_day", "observe_equity"}
    ]

    assert "reactivate" in clearing
    assert not hasattr(RiskState, "unhalt")
