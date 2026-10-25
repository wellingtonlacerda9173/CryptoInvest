from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

from api.services.trading import _QUIET_CODES, CHART_DECISIONS_MAX, _chart_decision
from engine.infrastructure.persistence.models import DecisionRow


def row(code: str, *, traded: bool = False) -> DecisionRow:
    fake = SimpleNamespace(
        id=7,
        decided_at=datetime(2026, 1, 1, tzinfo=UTC),
        code=code,
        traded=traded,
        strategy="elephant",
    )
    return cast("DecisionRow", cast("Any", fake))


def test_entered_decision_is_the_strong_tier() -> None:
    out = _chart_decision(row("approved", traded=True), None)
    assert out.tier == "entered" and out.label == "Entrou" and out.strategy == "elephant"


def test_refusals_are_blocked_and_keep_the_translated_sentence() -> None:
    out = _chart_decision(row("ai_veto"), None)
    assert out.tier == "blocked" and out.label == "Não entrou" and "assistente de IA" in out.text


def test_waiting_codes_are_quiet_and_unknown_codes_are_never_hidden() -> None:
    assert _chart_decision(row("no_signal"), None).tier == "quiet"
    assert "no_signal" in _QUIET_CODES and "ai_veto" not in _QUIET_CODES
    assert _chart_decision(row("codigo_novo"), None).tier == "blocked"


def test_limit_is_bounded() -> None:
    assert CHART_DECISIONS_MAX == 200
