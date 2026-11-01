"""Guarda do stub de regime: todo uso de `assess_regime` em engine/ precisa ser conhecido (e coberto por
`tests/unit/regime_stub.py`). Um uso novo faz esta suite falhar ate ser listado aqui e testado com o real."""

import sys
from pathlib import Path

from engine.application.risk import regime_filter
from tests.unit.regime_stub import _REAL

ENGINE = Path(__file__).resolve().parents[2] / "engine"
KNOWN = {
    "application/risk/regime_filter.py",
    "application/risk/gate.py",
    "application/risk/__init__.py",
    "runner/paper_robot.py",
}


def test_every_assess_regime_use_in_engine_is_known() -> None:
    users = {
        path.relative_to(ENGINE).as_posix()
        for path in ENGINE.rglob("*.py")
        if "assess_regime" in path.read_text(encoding="utf-8")
    }

    assert users == KNOWN


def test_stub_covers_every_loaded_module_holding_the_real_function() -> None:
    holders = {
        n
        for n, m in sys.modules.items()
        if n.startswith("engine.") and getattr(m, "assess_regime", None) is _REAL
    }

    assert {"engine.application.risk.gate", "engine.runner.paper_robot", "engine.application.risk"} <= holders
    assert regime_filter.assess_regime is _REAL
