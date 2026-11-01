"""Suites de comportamento (runner/backtest) usam series sinteticas curtas e planas, que o filtro de regime do
gate recusa por desenho. Elas fixam o regime em ALTA para exercitar o resto do fluxo; o filtro em si tem
testes proprios com o avaliador real (tests/unit/risk/test_regime_filter.py, test_regime_runner.py e
tests/unit/backtest/test_regime_real.py, que desligam o stub com `@pytest.mark.real_regime`).

O stub troca `assess_regime` na definicao e em TODO modulo `engine.*` ja importado que guarde uma referencia a
ela (`from ... import assess_regime`). `tests/unit/test_regime_stub_guard.py` falha se algum arquivo de
`engine/` usar `assess_regime` fora da lista conhecida: um novo ponto de uso nunca escapa do stub."""

import sys
from collections.abc import Iterator

import pytest

from engine.application.risk import regime_filter
from engine.application.risk.regime_filter import RegimeEvidence, RegimeVerdict

UPTREND = RegimeEvidence(verdict=RegimeVerdict.UPTREND, reason="fixture")
_REAL = regime_filter.assess_regime


def stub_uptrend(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> Iterator[None]:
    if request.node.get_closest_marker("real_regime") is not None:
        yield
        return
    for name, module in list(sys.modules.items()):
        if name.startswith("engine.") and getattr(module, "assess_regime", None) is _REAL:
            monkeypatch.setattr(module, "assess_regime", lambda *_a, **_k: UPTREND)
    yield
