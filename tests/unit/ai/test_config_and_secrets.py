from decimal import Decimal

import pytest
from pydantic import ValidationError

from engine.application.trading_config import ConfigDocument, diff_documents
from engine.application.trading_config.document import AiConfig
from engine.application.trading_config.risk_delta import cost_text
from engine.infrastructure.ai.factory import DISABLED, NO_KEY, build_analyst
from engine.infrastructure.config.logging import scrub_text
from engine.infrastructure.config.settings import Settings
from tests.unit.ai.support import FakeStore

KEY = "sk-ant-api03-TESTKEY1234567890"


def settings(tmp_path, *, key: bool) -> Settings:  # type: ignore[no-untyped-def]
    (tmp_path / "database_password").write_text("pw", encoding="utf-8")
    if key:
        (tmp_path / "anthropic_api_key").write_text(KEY + "\n", encoding="utf-8")
    return Settings(_secrets_dir=tmp_path)


def test_ai_is_off_by_default_with_the_cheapest_model_and_a_20_dollar_cap() -> None:
    ai = ConfigDocument().ai
    assert (ai.enabled, ai.model, ai.monthly_budget_usd, ai.timeout_seconds) == (
        False,
        "claude-haiku-5-5",
        Decimal(20),
        8.0,
    )


@pytest.mark.parametrize(
    "bad",
    [
        {"monthly_budget_usd": "201"},
        {"monthly_budget_usd": "-1"},
        {"timeout_seconds": 0},
        {"timeout_seconds": 16},
    ],
)
def test_hard_ceilings_are_enforced(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        AiConfig.model_validate(bad)


def test_budget_increase_is_described_as_cost_not_risk() -> None:
    old = ConfigDocument()
    new = old.with_section("ai", {**old.dump()["ai"], "monthly_budget_usd": "50"})
    [change] = diff_documents(old, new)
    assert not change.increases_risk
    assert "custo" in cost_text(change, Decimal(10_000)) and "US$ 50" in cost_text(change, Decimal(10_000))


def test_key_is_read_only_from_the_secret_file(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-env-999999")
    assert settings(tmp_path, key=False).anthropic_api_key is None
    loaded = settings(tmp_path, key=True).anthropic_api_key
    assert loaded is not None and loaded.get_secret_value() == KEY
    assert KEY not in repr(loaded) and KEY not in repr(settings(tmp_path, key=True))


def test_key_is_scrubbed_from_log_text() -> None:
    assert KEY not in scrub_text(f"falha com chave {KEY} na chamada")


def test_factory_degrades_without_key_or_when_disabled(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = FakeStore()
    keyless = tmp_path / "keyless"
    keyless.mkdir()
    assert build_analyst(AiConfig(), settings(tmp_path, key=True), store).status == DISABLED
    off = build_analyst(AiConfig(enabled=True), settings(keyless, key=False), store)
    assert off.analyst is None and off.status == NO_KEY


def test_factory_builds_the_analyst_with_key(tmp_path) -> None:  # type: ignore[no-untyped-def]
    built = build_analyst(AiConfig(enabled=True), settings(tmp_path, key=True), FakeStore())
    assert built.analyst is not None and built.status == "ligada"
