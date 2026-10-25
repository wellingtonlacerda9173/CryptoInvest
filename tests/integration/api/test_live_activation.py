"""Trava de ativacao do Live (POST /live/activate): so grava a AUTORIZACAO append-only quando TODAS as
condicoes valem NO SERVIDOR. Banco descartavel `*_test`; nada de Live e ligado."""

import asyncio
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import URL, func, select, text
from sqlalchemy.exc import DBAPIError

from api.binance_keys import KeyPermissions
from engine.application.live_authorization import (
    LIVE_PHRASE,
    authorization_hash,
    authorization_mac_valid,
    authorization_record,
)
from engine.infrastructure.persistence.models import (
    AuditLogRow,
    LiveAuthorizationRevocationRow,
    LiveAuthorizationRow,
    RunRow,
)
from engine.infrastructure.persistence.session import create_engine, create_session_factory
from tests.api_support import LIVE_MAC_KEY
from tests.integration.api.conftest import Harness, Session, fake_key
from tests.integration.api.live_support import GOOD, seed_paper_run
from tests.integration.api.runner_support import (
    fake_link,
    module_db_url,
    module_sessions,
    rh,
    rh_admin,
    rh_reader,
    seed_run,
)

pytestmark = pytest.mark.integration

VARIANTS: dict[str, dict[str, Any]] = {
    "paper-live-d59": {"days": 59},
    "paper-live-t29": {"trades": 29, "losses": 6},
    "paper-live-l4": {"losses": 4},
    "paper-live-pf": {"win": "10"},
    "paper-live-dl": {"big_loss": "-150"},
    "paper-live-open": {"open_calendar": True},
}


@pytest.fixture(scope="module")
def seeded(module_db_url: URL) -> None:
    async def go() -> None:
        engine = create_engine(module_db_url)
        try:
            sessions = create_session_factory(engine)
            await seed_run(sessions)
            await seed_paper_run(sessions, GOOD)
            for run_id, kwargs in VARIANTS.items():
                await seed_paper_run(sessions, run_id, **kwargs)
        finally:
            await engine.dispose()

    asyncio.run(go())


def body(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "phrase": LIVE_PHRASE,
        "confirm_real_money": True,
        "initial_capital_quote": "50",
        "reason": "primeira ativacao com capital reduzido",
        "run_id": GOOD,
    }
    return {k: v for k, v in (base | overrides).items() if v is not None}


async def add_key(admin: Session, label: str = "live") -> str:
    key, secret = fake_key()
    created = await admin.send(
        "POST", "/credentials", json={"label": f"{label}-{key[-6:]}", "api_key": key, "api_secret": secret}
    )
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


def codes(response: Any) -> set[str]:
    return {e["code"] for e in response.json()["errors"]}


async def count(rh: Harness, model: Any) -> int:
    async with rh.sessions() as db:
        return int(await db.scalar(select(func.count()).select_from(model)) or 0)


async def refusals(rh: Harness) -> list[dict[str, Any]]:
    async with rh.sessions() as db:
        rows = await db.scalars(
            select(AuditLogRow)
            .where(AuditLogRow.action == "live.activation.refused")
            .order_by(AuditLogRow.id)
        )
        return [r.payload for r in rows]


# --------------------------------------------------------------------------- papeis, CSRF e reauth


async def test_roles_csrf_and_reauth_are_enforced_before_anything_is_evaluated(
    rh: Harness, rh_admin: Session, rh_reader: Session, seeded: None
) -> None:
    anonymous = rh.client()
    assert (await anonymous.post("/live/activate", json=body())).status_code == 401
    assert (await anonymous.get("/live/authorization")).status_code == 401
    assert (await rh_reader.send("POST", "/live/activate", json=body())).status_code == 403
    assert (
        await rh_reader.send("POST", "/live/authorization/revoke", json={"reason": "teste"})
    ).status_code == 403
    without_csrf = await rh_admin.client.post("/live/activate", json=body())
    assert without_csrf.status_code == 403
    reader_state = await rh_reader.get("/live/authorization")
    assert reader_state.status_code == 200 and reader_state.json()["live_available"] is False
    before = len(await refusals(rh))
    rh.clock.advance(minutes=6)
    stale = await rh_admin.send("POST", "/live/activate", json=body())
    assert stale.status_code == 403 and stale.json()["type"].endswith("fresh-auth-required")
    assert len(await refusals(rh)) == before
    assert await count(rh, LiveAuthorizationRow) == 0


# --------------------------------------------------------------------------- criterios nao atendidos


async def test_current_robot_is_refused_with_409_and_every_unmet_criterion_is_listed_and_audited(
    rh: Harness, rh_admin: Session, seeded: None
) -> None:
    await add_key(rh_admin)
    response = await rh_admin.send("POST", "/live/activate", json=body(run_id=None))

    assert response.status_code == 409
    assert {"operating_days", "min_trades", "gate_not_approved"} <= codes(response)
    audited = (await refusals(rh))[-1]
    assert audited["result"] == "denied"
    assert {u["criterion"] for u in audited["details"]["unmet"]} == codes(response)
    assert all(u["message"] for u in audited["details"]["unmet"])
    assert await count(rh, LiveAuthorizationRow) == 0
    state = (await rh_admin.get("/live/authorization")).json()
    assert state["authorization"] is None and state["stage"] == "no_authorization"


@pytest.mark.parametrize(
    ("run_id", "expected"),
    [
        ("paper-live-d59", "operating_days"),
        ("paper-live-t29", "min_trades"),
        ("paper-live-l4", "min_losses"),
        ("paper-live-pf", "profit_factor"),
        ("paper-live-dl", "daily_loss"),
        ("paper-live-open", "ineligible_run"),
        ("run-inexistente", "no_test"),
    ],
)
async def test_each_minimum_criterion_alone_blocks_the_activation(
    rh: Harness, rh_admin: Session, seeded: None, run_id: str, expected: str
) -> None:
    await add_key(rh_admin)

    response = await rh_admin.send("POST", "/live/activate", json=body(run_id=run_id))

    assert response.status_code == 409
    if expected == "no_test":
        assert "no_test" in codes(response)
    else:
        assert codes(response) == {expected, "gate_not_approved"}, codes(response)
    assert await count(rh, LiveAuthorizationRow) == 0


async def test_the_qualified_run_alone_passes_every_paper_criterion(
    rh: Harness, rh_admin: Session, seeded: None
) -> None:
    await add_key(rh_admin)
    response = await rh_admin.send("POST", "/live/activate", json=body(phrase="x"))

    assert codes(response) == {"phrase"}


@pytest.mark.parametrize(
    "phrase",
    [None, "", " OPERAR COM DINHEIRO REAL", "OPERAR COM DINHEIRO REAL ", "operar com dinheiro real",
     "OPERAR  COM DINHEIRO REAL", "OPERAR COM DINHEIRO REAL\n", "OPERAR COM DINHEIRO REAL."],
)  # fmt: skip
async def test_phrase_must_match_exactly(
    rh: Harness, rh_admin: Session, seeded: None, phrase: str | None
) -> None:
    await add_key(rh_admin)

    response = await rh_admin.send("POST", "/live/activate", json=body(phrase=phrase))

    assert response.status_code == 409 and codes(response) == {"phrase"}
    assert await count(rh, LiveAuthorizationRow) == 0


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"confirm_real_money": False}, "confirm_real_money"),
        ({"confirm_real_money": None}, "confirm_real_money"),
        ({"initial_capital_quote": None}, "initial_capital"),
        ({"initial_capital_quote": "0"}, "initial_capital"),
        ({"initial_capital_quote": "100.01"}, "initial_capital_above_cap"),
        ({"initial_capital_quote": "100000"}, "initial_capital_above_cap"),
        ({"reason": None}, "reason"),
    ],
)
async def test_confirmation_capital_and_reason_are_mandatory(
    rh: Harness, rh_admin: Session, seeded: None, overrides: dict[str, Any], expected: str
) -> None:
    await add_key(rh_admin)

    response = await rh_admin.send("POST", "/live/activate", json=body(**overrides))

    assert response.status_code == 409 and codes(response) == {expected}


# --------------------------------------------------------------------------- chave da Binance


async def test_without_a_saved_key_activation_is_refused(
    rh: Harness, rh_admin: Session, seeded: None
) -> None:
    for item in (await rh_admin.get("/credentials")).json():
        await rh_admin.send("DELETE", f"/credentials/{item['id']}")

    response = await rh_admin.send("POST", "/live/activate", json=body())

    assert response.status_code == 409 and codes(response) == {"key_missing"}


@pytest.mark.parametrize(
    ("permissions", "expected"),
    [
        (KeyPermissions(can_read=True, can_trade=True, can_withdraw=True, ip_restricted=True), "key_withdraw"),
        (KeyPermissions(can_read=True, can_trade=True, can_withdraw=False, ip_restricted=False), "key_ip_unrestricted"),
        (KeyPermissions(can_read=True, can_trade=True, can_withdraw=False, ip_restricted=None), "key_ip_unrestricted"),
        (KeyPermissions(can_read=True, can_trade=False, can_withdraw=False, ip_restricted=True), "key_cannot_trade"),
        (
            KeyPermissions(can_read=True, can_trade=True, can_withdraw=False, ip_restricted=True, margin_enabled=True),
            "key_warnings",
        ),
        (
            KeyPermissions(can_read=True, can_trade=True, can_withdraw=False, ip_restricted=True, futures_enabled=True),
            "key_warnings",
        ),
    ],
)  # fmt: skip
async def test_key_must_be_reverified_without_withdrawal_and_with_ip_restriction(
    rh: Harness, rh_admin: Session, seeded: None, permissions: KeyPermissions, expected: str
) -> None:
    await add_key(rh_admin)
    rh.verifier.permissions = permissions

    try:
        response = await rh_admin.send("POST", "/live/activate", json=body())
    finally:
        rh.verifier.permissions = KeyPermissions(
            can_read=True, can_trade=True, can_withdraw=False, ip_restricted=True
        )

    assert response.status_code == 409 and codes(response) == {expected}


async def test_key_that_cannot_be_reverified_blocks(rh: Harness, rh_admin: Session, seeded: None) -> None:
    await add_key(rh_admin)
    rh.verifier.error = "A Binance recusou a chave."
    try:
        response = await rh_admin.send("POST", "/live/activate", json=body())
    finally:
        rh.verifier.error = None

    assert response.status_code == 409 and "key_unverified" in codes(response)


async def test_a_running_live_run_blocks_a_new_authorization(
    rh: Harness, rh_admin: Session, seeded: None
) -> None:
    await add_key(rh_admin)
    async with rh.sessions.begin() as db:
        db.add(
            RunRow(
                run_id="live-running",
                mode="live",
                started_at=rh.clock(),
                config_hash="h" * 32,
                code_version="x",
            )
        )

    response = await rh_admin.send("POST", "/live/activate", json=body())

    async with rh.sessions.begin() as db:
        run = await db.get(RunRow, "live-running")
        assert run is not None
        run.ended_at = rh.clock()
    assert response.status_code == 409 and codes(response) == {"live_run_active"}


# --------------------------------------------------------------------------- sucesso, revogacao e etapas


async def test_only_when_everything_holds_the_authorization_is_written_and_nothing_starts(
    rh: Harness, rh_admin: Session, rh_reader: Session, seeded: None
) -> None:
    credential_id = await add_key(rh_admin)
    status_before = (await rh_admin.get("/status")).json()
    commands_before = (await rh_admin.get("/control/commands", params={"limit": 100})).json()["items"]

    granted = await rh_admin.send("POST", "/live/activate", json=body())

    assert granted.status_code == 201, granted.text
    data = granted.json()
    assert (
        data["active"] is True
        and data["paper_run_id"] == GOOD
        and Decimal(data["initial_capital_quote"]) == 50
    )
    async with rh.sessions() as db:
        row = await db.get(LiveAuthorizationRow, data["id"])
        assert row is not None
        assert row.confirmed_phrase == LIVE_PHRASE and row.credential_id == credential_id
        assert row.expires_at - row.created_at == timedelta(hours=24)
        assert row.criteria["gate"] and row.criteria["metrics"]["operating_days"] >= 60
        record = authorization_record(
            authorization_id=row.id,
            paper_run_id=row.paper_run_id,
            criteria=row.criteria,
            actor=row.actor,
            reason=row.reason,
            confirmed_phrase=row.confirmed_phrase,
            initial_capital_quote=row.initial_capital_quote,
            credential_id=row.credential_id,
            key_permissions=row.key_permissions,
            created_at=row.created_at,
            expires_at=row.expires_at,
        )
        key = LIVE_MAC_KEY.encode()
        assert len(row.record_hash) == 64 and authorization_mac_valid(key, record, row.record_hash)
        assert row.record_hash != authorization_hash(record)
        assert not authorization_mac_valid(key, record | {"actor": "intruso"}, row.record_hash)
    status_after = (await rh_admin.get("/status")).json()
    assert (
        status_after["mode"] == status_before["mode"] == "paper"
        and status_after["live"]["live_available"] is False
    )
    assert (await rh_admin.get("/control/commands", params={"limit": 100})).json()["items"] == commands_before
    audited = (await rh_admin.get("/audit", params={"action": "live.authorization", "limit": 50})).json()[
        "items"
    ]
    assert [e["action"] for e in audited] == ["live.authorization.granted"]
    assert (await rh_reader.get("/live/authorization")).json()["authorization"]["id"] == data["id"]
    state = (await rh_admin.get("/live/authorization")).json()
    assert state["stage"] == "authorized_waiting_start" and state["limit_increase_allowed"] is False
    assert (
        state["reduced_capital_days"] == 7
        and state["limit_increase_days"] == 30
        and state["live_available"] is False
    )

    again = await rh_admin.send("POST", "/live/activate", json=body())
    assert again.status_code == 409 and "authorization_active" in codes(again)

    assert (
        await rh_reader.send("POST", "/live/authorization/revoke", json={"reason": "nao pode"})
    ).status_code == 403
    revoked = await rh_admin.send(
        "POST", "/live/authorization/revoke", json={"reason": "revisao manual do risco"}
    )
    assert revoked.status_code == 200 and revoked.json()["active"] is False
    assert revoked.json()["revoked_by"] == rh_admin.username
    assert (
        await rh_admin.send("POST", "/live/authorization/revoke", json={"reason": "de novo"})
    ).status_code == 404
    assert (await rh_admin.get("/live/authorization")).json()["stage"] == "no_authorization"
    assert await count(rh, LiveAuthorizationRevocationRow) == 1
    regranted = await rh_admin.send("POST", "/live/activate", json=body())
    assert regranted.status_code == 201 and regranted.json()["id"] != data["id"]


async def test_authorization_tables_are_append_only(rh: Harness, seeded: None) -> None:
    async with rh.sessions() as db:
        first = await db.scalar(select(LiveAuthorizationRow.id).limit(1))
    if first is None:
        pytest.skip("depende do teste de sucesso ter rodado antes")
    for statement in (
        "UPDATE live_authorizations SET reason = 'adulterado'",
        "DELETE FROM live_authorizations",
        "TRUNCATE live_authorizations",
        "UPDATE live_authorization_revocations SET reason = 'adulterado'",
        "DELETE FROM live_authorization_revocations",
    ):
        with pytest.raises(DBAPIError):
            async with rh.sessions.begin() as db:
                await db.execute(text(statement))
