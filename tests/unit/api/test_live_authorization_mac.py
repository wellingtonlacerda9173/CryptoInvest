"""Reverificacao do HMAC do registro da autorizacao do Live pelo consumidor (sem banco)."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr

from api.services.live_activation import LiveActivationService
from engine.application.live_authorization import LIVE_PHRASE, authorization_mac, authorization_record
from engine.infrastructure.persistence.models import LiveAuthorizationRow
from tests.api_support import LIVE_MAC_KEY, make_settings

NOW = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


def service(**settings: Any) -> LiveActivationService:
    container: Any = SimpleNamespace(settings=make_settings(**settings))
    return LiveActivationService(container, audit=None)  # type: ignore[arg-type]


def signed_row(key: str = LIVE_MAC_KEY) -> LiveAuthorizationRow:
    fields: dict[str, Any] = {
        "id": uuid.uuid4().hex,
        "paper_run_id": "paper-1",
        "criteria": {"gate": [{"name": "dias", "ok": True}]},
        "actor": "admin",
        "reason": "ativar o live",
        "confirmed_phrase": LIVE_PHRASE,
        "initial_capital_quote": Decimal("50.0000000000"),
        "credential_id": str(uuid.uuid4()),
        "key_permissions": {"can_trade": True, "can_withdraw": False},
        "created_at": NOW,
        "expires_at": NOW + timedelta(hours=24),
    }
    record = authorization_record(
        authorization_id=fields["id"], **{k: v for k, v in fields.items() if k != "id"}
    )
    return LiveAuthorizationRow(**fields, record_hash=authorization_mac(key.encode(), record))


def test_an_untouched_row_is_intact() -> None:
    assert service().row_intact(signed_row())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("initial_capital_quote", Decimal(5000)),
        ("credential_id", "outra-chave"),
        ("actor", "intruso"),
        ("expires_at", NOW + timedelta(days=3650)),
        ("criteria", {"gate": []}),
    ],
)
def test_a_tampered_row_is_not_intact(field: str, value: Any) -> None:
    row = signed_row()
    setattr(row, field, value)

    assert not service().row_intact(row)


def test_a_row_signed_with_another_key_or_without_any_key_is_not_intact() -> None:
    row = signed_row("outra-chave-de-assinatura-com-mais-de-32-bytes")

    assert not service().row_intact(row)
    assert not service(live_authorization_hmac_key=None).row_intact(signed_row())


def test_the_command_key_is_the_fallback_when_there_is_no_dedicated_key() -> None:
    fallback = "c" * 48

    assert service(live_authorization_hmac_key=None, command_hmac_key=SecretStr(fallback)).row_intact(
        signed_row(fallback)
    )
