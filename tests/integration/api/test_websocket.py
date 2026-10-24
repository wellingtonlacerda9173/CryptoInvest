"""WebSocket /ws de ponta a ponta (Starlette TestClient + Postgres real + Redis fake): sessao, Origin, papel,
canais, retomada, ping, limite por usuario, sessao revogada, mensagens invalidas e aviso de desconexao."""

import asyncio
import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import URL
from starlette.testclient import TestClient, WebSocketTestSession
from starlette.websockets import WebSocketDisconnect

from api.app import create_app
from api.binance_keys import FakeKeyVerifier
from tests.api_support import GOOD_PASSWORD, FakeClock, make_settings
from tests.integration.api.conftest import Harness
from tests.redis_fakes import FakeLink, FakeSource

pytestmark = pytest.mark.integration

ORIGIN = "http://localhost:3000"


class Ws:
    """Uma API a parte (com o mesmo banco do `harness`) dentro de um TestClient, e atalhos para o canal."""

    def __init__(self, url: URL, **settings: Any) -> None:
        self.source = FakeSource()
        self.link = FakeLink(self.source)
        self.clock = FakeClock()
        self.app: FastAPI = create_app(
            make_settings(**settings),
            database_url=url,
            verifier=FakeKeyVerifier(),
            link=self.link,
            events=self.source,
        )

    def push(self, client: TestClient, kind: str, payload: dict[str, Any] | None = None) -> None:
        assert client.portal is not None
        client.portal.call(lambda: self.source.push(kind, payload or {"kind": kind}))

    def hub(self) -> Any:
        return self.app.state.container.hub


@contextmanager
def client_for(url: URL, **settings: Any) -> Iterator[tuple[TestClient, Ws]]:
    ws = Ws(url, **settings)
    with TestClient(ws.app) as client:
        yield client, ws


def connect(
    client: TestClient, token: str | None, *, origin: str | None = ORIGIN, query: str = ""
) -> WebSocketTestSession:
    headers: dict[str, str] = {}
    if origin is not None:
        headers["origin"] = origin
    if token is not None:
        headers["cookie"] = f"ci_session={token}"
    return client.websocket_connect(f"/ws{query}", headers=headers)


def receive_until(session: WebSocketTestSession, kind: str, limit: int = 20) -> dict[str, Any]:
    for _ in range(limit):
        message = json.loads(session.receive_text())
        if message["type"] == kind:
            return message  # type: ignore[no-any-return]
    raise AssertionError(f"nenhuma mensagem {kind}")


def events(session: WebSocketTestSession, count: int, limit: int = 30) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for _ in range(limit):
        message = json.loads(session.receive_text())
        if message["type"] == "event":
            found.append(message)
            if len(found) == count:
                return found
    raise AssertionError(f"so {len(found)} de {count} eventos")


def runner_state(session: WebSocketTestSession, connected: bool, limit: int = 6) -> dict[str, Any]:
    """Le avisos `runner` ate achar o estado esperado (o primeiro aviso vem junto do `hello`)."""
    for _ in range(limit):
        message = receive_until(session, "runner")
        if message["connected"] is connected:
            return message
    raise AssertionError(f"nenhum aviso runner connected={connected}")


def subscribe(session: WebSocketTestSession, channels: list[str], last: str | None = None) -> dict[str, Any]:
    session.send_text(json.dumps({"type": "subscribe", "channels": channels, "last_event_id": last}))
    return receive_until(session, "subscribed")


async def login_token(harness: Harness, role: str) -> tuple[str, str]:
    session = await harness.login(await harness.create_user(role))
    token = session.client.cookies.get("ci_session")
    assert token
    return token, session.username


def closes(client: TestClient, token: str | None, **kwargs: Any) -> int:
    """Codigo de fechamento quando o servidor recusa a conexao (antes ou logo depois do aceite)."""
    with pytest.raises(WebSocketDisconnect) as caught, connect(client, token, **kwargs) as session:
        session.receive_text()
    return int(caught.value.code)


# --------------------------------------------------------------------------- entrada


async def test_connection_requires_a_verified_session_a_trusted_origin_and_a_known_role(
    harness: Harness, scratch_url: URL
) -> None:
    token, _ = await login_token(harness, "admin")
    pending = harness.client()
    first = await pending.post(
        "/auth/login", json={"username": await harness.create_user("reader"), "password": GOOD_PASSWORD}
    )
    assert first.status_code == 200
    pending_token = pending.cookies.get("ci_session")
    with client_for(scratch_url) as (client, _):
        assert closes(client, None) == 1008
        assert closes(client, "lixo") == 1008
        assert closes(client, token + "x") == 1008
        assert closes(client, pending_token) == 1008
        assert closes(client, token, origin="https://evil.example") == 1008
        assert closes(client, token, origin=None) == 1008
        assert closes(client, token, origin="http://localhost:3000.evil.example") == 1008
        with connect(client, token) as ok:
            assert json.loads(ok.receive_text())["type"] == "hello"
    await pending.aclose()


async def test_admin_and_reader_both_connect_and_get_a_hello_with_the_contract(
    harness: Harness, scratch_url: URL
) -> None:
    admin, _ = await login_token(harness, "admin")
    reader, _ = await login_token(harness, "reader")
    with client_for(scratch_url, ws_ping_seconds=7) as (client, ws):
        for token, role in ((admin, "admin"), (reader, "reader")):
            with connect(client, token) as session:
                hello = json.loads(session.receive_text())
                assert hello["type"] == "hello" and hello["role"] == role and hello["ping_seconds"] == 7
                assert hello["epoch"] == ws.hub().epoch and "decisions" in hello["channels"]
                assert hello["runner_connected"] is False
                notice = json.loads(session.receive_text())
                assert (
                    notice["type"] == "runner"
                    and notice["connected"] is False
                    and "Desconectado do robô" in notice["message"]
                )


# --------------------------------------------------------------------------- canais


async def test_only_subscribed_channels_arrive_and_names_map_to_the_panel_vocabulary(
    harness: Harness, scratch_url: URL
) -> None:
    token, _ = await login_token(harness, "reader")
    with client_for(scratch_url) as (client, ws), connect(client, token) as session:
        receive_until(session, "hello")
        assert subscribe(session, ["status", "alerts"])["channels"] == ["alerts", "status"]
        ws.push(client, "decisions", {"code": "no_signal"})
        ws.push(client, "state", {"state": "RUNNING"})
        ws.push(client, "risk", {"action": "daily_loss_pause"})
        ws.push(client, "orders", {"event": "x"})
        got = events(session, 2)
        assert [m["channel"] for m in got] == ["status", "alerts"]
        assert got[0]["data"]["state"] == "RUNNING" and got[0]["id"].startswith(ws.hub().epoch + ":")
        session.send_text(json.dumps({"type": "unsubscribe", "channels": ["status"]}))
        assert receive_until(session, "subscribed")["channels"] == ["alerts"]
        ws.push(client, "state", {"state": "PAUSED"})
        ws.push(client, "risk", {"action": "daily_target_reached"})
        assert events(session, 1)[0]["data"]["action"] == "daily_target_reached"


async def test_channels_can_be_chosen_in_the_url(harness: Harness, scratch_url: URL) -> None:
    token, _ = await login_token(harness, "reader")
    with (
        client_for(scratch_url) as (client, ws),
        connect(client, token, query="?channels=equity,desconhecido") as session,
    ):
        receive_until(session, "hello")
        ws.push(client, "equity", {"equity": "10000"})
        ws.push(client, "trades", {"x": 1})
        assert events(session, 1)[0]["channel"] == "equity"


async def test_reconnecting_with_last_event_id_replays_what_was_missed(
    harness: Harness, scratch_url: URL
) -> None:
    token, _ = await login_token(harness, "reader")
    with client_for(scratch_url) as (client, ws):
        with connect(client, token) as first:
            receive_until(first, "hello")
            subscribe(first, ["decisions"])
            ws.push(client, "decisions", {"n": 1})
            last = events(first, 1)[0]["id"]
        ws.push(client, "decisions", {"n": 2})
        ws.push(client, "decisions", {"n": 3})
        ws.push(client, "orders", {"n": 99})
        for _ in range(250):
            if ws.hub().last_event_id().rpartition(":")[2] == "4":
                break
            await asyncio.sleep(0.02)
        with connect(client, token) as second:
            receive_until(second, "hello")
            ack = subscribe(second, ["decisions"], last)
            assert ack["resumed"] is True and ack["replayed"] == 2
            assert [m["data"]["n"] for m in events(second, 2)] == [2, 3]
        with connect(client, token) as third:
            receive_until(third, "hello")
            session = subscribe(third, ["decisions"], "epoca-velha:1")
            assert session["resumed"] is False
            assert receive_until(third, "resync")["reason"].startswith("Não foi possível retomar")


# --------------------------------------------------------------------------- vida da conexao


async def test_runner_disconnection_and_recovery_are_announced(harness: Harness, scratch_url: URL) -> None:
    token, _ = await login_token(harness, "reader")
    with client_for(scratch_url) as (client, ws), connect(client, token) as session:
        receive_until(session, "hello")
        subscribe(session, ["heartbeat"])
        hub = ws.hub()
        hub._clock = ws.clock
        ws.push(client, "heartbeat", {"state": "RUNNING"})
        runner_state(session, True)
        ws.clock.advance(seconds=200)
        assert client.portal is not None
        client.portal.call(hub.check_stale)
        gone = runner_state(session, False)
        assert "Desconectado do robô" in gone["message"]
        ws.push(client, "heartbeat", {"state": "RUNNING"})
        runner_state(session, True)


async def test_server_pings_and_drops_a_client_that_never_answers(harness: Harness, scratch_url: URL) -> None:
    token, _ = await login_token(harness, "reader")
    with client_for(scratch_url, ws_ping_seconds=0.2) as (client, _):
        with connect(client, token) as polite:
            receive_until(polite, "hello")
            for _ in range(3):
                assert receive_until(polite, "ping")["at"]
                polite.send_text(json.dumps({"type": "pong"}))
        with pytest.raises(WebSocketDisconnect) as caught, connect(client, token) as rude:
            for _ in range(40):
                rude.receive_text()
        assert caught.value.code == 1008


async def test_connections_per_user_are_limited_and_released_on_close(
    harness: Harness, scratch_url: URL
) -> None:
    token, _ = await login_token(harness, "reader")
    other, _ = await login_token(harness, "reader")
    with client_for(scratch_url, ws_max_connections_per_user=2) as (client, ws):
        with connect(client, token) as a, connect(client, token) as b:
            receive_until(a, "hello")
            receive_until(b, "hello")
            assert closes(client, token) == 1008
            with connect(client, other) as stranger:
                assert receive_until(stranger, "hello")
        for _ in range(150):
            if not ws.hub().clients:
                break
            await asyncio.sleep(0.02)
        with connect(client, token) as again:
            assert receive_until(again, "hello")


async def test_revoking_the_session_closes_the_socket(harness: Harness, scratch_url: URL) -> None:
    admin = await harness.login(await harness.create_user("admin"))
    token = admin.client.cookies.get("ci_session")
    assert token
    with client_for(scratch_url, ws_session_check_seconds=0.2, ws_ping_seconds=0.3) as (client, _):
        with pytest.raises(WebSocketDisconnect) as caught, connect(client, token) as session:
            receive_until(session, "hello")
            assert (await admin.send("DELETE", "/sessions")).status_code in {200, 204}
            for _ in range(60):
                session.receive_text()
        assert caught.value.code == 1008
    await admin.aclose()


async def test_a_disabled_user_loses_the_socket(harness: Harness, scratch_url: URL) -> None:
    boss = await harness.login(await harness.create_user("admin"))
    username = await harness.create_user("reader")
    reader = await harness.login(username)
    token = reader.client.cookies.get("ci_session")
    assert token
    users = (await boss.get("/users")).json()
    target = next(u for u in users if u["username"] == username)
    with client_for(scratch_url, ws_session_check_seconds=0.2, ws_ping_seconds=0.3) as (client, _):
        with pytest.raises(WebSocketDisconnect) as caught, connect(client, token) as session:
            receive_until(session, "hello")
            patched = await boss.send("PATCH", f"/users/{target['id']}", json={"disabled": True})
            assert patched.status_code == 200
            for _ in range(60):
                session.receive_text()
        assert caught.value.code == 1008
    await boss.aclose()
    await reader.aclose()


# --------------------------------------------------------------------------- mensagens do cliente


async def test_the_channel_is_read_only_and_bad_messages_are_answered_then_the_socket_is_closed(
    harness: Harness, scratch_url: URL
) -> None:
    token, _ = await login_token(harness, "admin")
    with client_for(scratch_url) as (client, ws):
        with connect(client, token) as session:
            receive_until(session, "hello")
            for text in (
                '{"type":"pause"}',
                "nao-json",
                '{"type":"subscribe","channels":["admin"]}',
                '{"type":"subscribe","channels":[]}',
            ):
                session.send_text(text)
                assert receive_until(session, "error")["code"] == "invalid_message"
            before = ws.hub().last_event_id()
            session.send_text('{"type":"pong"}')
            assert ws.hub().last_event_id() == before
        with pytest.raises(WebSocketDisconnect) as spam, connect(client, token) as session:
            receive_until(session, "hello")
            for _ in range(30):
                session.send_text("lixo")
            for _ in range(60):
                session.receive_text()
        assert spam.value.code == 1008
        with pytest.raises(WebSocketDisconnect) as big, connect(client, token) as session:
            receive_until(session, "hello")
            session.send_text("x" * 5000)
            for _ in range(10):
                session.receive_text()
        assert big.value.code == 1009
