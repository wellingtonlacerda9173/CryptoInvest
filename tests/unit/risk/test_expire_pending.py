from datetime import timedelta

import pytest

from engine.application.risk import ApprovedEntry, RiskState, evaluate_entry, expire_pending
from engine.domain.risk import RiskConfig
from tests.unit.risk.factories import EMPTY_CALENDAR, NOW, D, history, market, rules, signal


def _approved() -> ApprovedEntry:
    out = evaluate_entry(
        signal(target="111"),
        state=RiskState.initial(D(10_000), NOW),
        config=RiskConfig(),
        rules=rules(),
        positions=[],
        pending=[],
        market=market(),
        history=history(),
        calendar=EMPTY_CALENDAR,
        now=NOW,
        client_order_id="p-1",
    )
    assert isinstance(out, ApprovedEntry)
    return out


def test_splits_active_and_expired_with_the_boundary_still_active() -> None:
    approved = _approved()
    expires = approved.guards.expires_at

    assert expire_pending([approved], expires) == ((approved,), ())
    assert expire_pending([approved], expires + timedelta(seconds=1)) == ((), (approved,))


def test_empty_pending_and_naive_time() -> None:
    assert expire_pending([], NOW) == ((), ())
    with pytest.raises(ValueError, match="timezone-aware"):
        expire_pending([], NOW.replace(tzinfo=None))
