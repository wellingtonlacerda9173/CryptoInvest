"""WebSocket `/ws`: eventos do robô em tempo real. Contrato das mensagens em `api/ws_schema.py`.

Segurança: só abre com cookie de sessão válido (senha + 2FA), `Origin` do painel e papel conhecido; limite de
conexões por usuário; a sessão é revalidada de tempos em tempos (revogada ou vencida = conexão encerrada).
Nenhuma mensagem do cliente muda estado: o canal é somente leitura (comandos vão por REST, com CSRF).
"""

import asyncio
import contextlib
import time
from collections import OrderedDict
from datetime import datetime
from typing import Any

import structlog
from fastapi import APIRouter, WebSocket
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select
from starlette.websockets import WebSocketDisconnect, WebSocketState

from api.container import Container
from api.persistence.models import SessionRow, UserRow
from api.services.realtime import RealtimeHub, Subscription, event_seq, frame
from api.services.sessions import Principal, SessionService
from api.ws_schema import (
    CHANNELS,
    CLOSE_POLICY,
    CLOSE_TOO_BIG,
    CLOSE_TRY_AGAIN,
    ChannelName,
    ClientMessage,
    ErrorMessage,
    HelloMessage,
    PingMessage,
    PongMessage,
    SubscribedMessage,
    SubscribeMessage,
    UnsubscribeMessage,
)

log = structlog.get_logger()

router = APIRouter()

ROLES = frozenset({"admin", "reader"})
MAX_CLIENT_MESSAGE = 4096
SENT_MEMORY = 4096
MAX_INVALID_MESSAGES = 10
MAX_MESSAGES_PER_WINDOW = 40
RATE_WINDOW_SECONDS = 10.0
_CLIENT = TypeAdapter(ClientMessage)


class Connection:
    """Um cliente: serializa os envios (uma tarefa por vez) e aplica o tempo limite de escrita."""

    def __init__(self, ws: WebSocket, sub: Subscription, timeout: float) -> None:
        self.ws = ws
        self.sub = sub
        self._timeout = timeout
        self._lock = asyncio.Lock()
        self.last_seen = time.monotonic()
        self._sent: OrderedDict[int, None] = OrderedDict()

    def first_delivery(self, text: str) -> bool:
        """Falso se este evento (mesmo id) já foi enviado a esta conexão (retomada x tempo real)."""
        seq = event_seq(text)
        if seq is None:
            return True
        if seq in self._sent:
            return False
        self._sent[seq] = None
        while len(self._sent) > SENT_MEMORY:
            self._sent.popitem(last=False)
        return True

    async def send(self, text: str) -> None:
        async with self._lock:
            await asyncio.wait_for(self.ws.send_text(text), self._timeout)

    async def send_burst(self, texts: list[str]) -> None:
        """Envia a sequencia SEM intercalar o escritor: eventos ao vivo nunca ultrapassam a retomada."""
        async with self._lock:
            for text in texts:
                await asyncio.wait_for(self.ws.send_text(text), self._timeout)

    async def close(self, code: int, reason: str = "") -> None:
        with contextlib.suppress(RuntimeError, OSError):
            if self.ws.application_state is WebSocketState.CONNECTED:
                await self.ws.close(code=code, reason=reason[:100])


async def session_alive(container: Container, principal: Principal) -> bool:
    """Revalida SEM renovar a sessão (a tela aberta não mantém a sessão viva por inatividade)."""
    now = container.clock()
    async with container.sessions() as db:
        found = (
            await db.execute(
                select(SessionRow, UserRow)
                .join(UserRow, UserRow.id == SessionRow.user_id)
                .where(SessionRow.id == principal.session_id)
            )
        ).first()
    if found is None:
        return False
    session, user = found
    return (
        session.revoked_at is None
        and now < session.expires_at
        and now < session.absolute_expires_at
        and not user.disabled
        and user.role in ROLES
    )


def _parse_channels(raw: str | None) -> set[ChannelName]:
    names = {part.strip() for part in (raw or "").split(",") if part.strip()}
    return {c for c in CHANNELS if c in names}


@router.websocket("/ws")
async def stream(websocket: WebSocket) -> None:
    container: Container = websocket.app.state.container
    hub = container.hub
    settings = container.settings
    origin = websocket.headers.get("origin")
    if origin is None or origin.rstrip("/") not in settings.panel_origins:
        await websocket.close(code=CLOSE_POLICY)
        return
    principal = await SessionService(container).authenticate(
        websocket.cookies.get(settings.session_cookie_name), verified=True
    )
    if principal is None or principal.role not in ROLES:
        await websocket.close(code=CLOSE_POLICY)
        return
    if hub is None:
        await websocket.close(code=CLOSE_TRY_AGAIN)
        return
    sub = hub.register(str(principal.user_id), settings.ws_max_connections_per_user)
    if sub is None:
        await websocket.close(code=CLOSE_POLICY, reason="muitas conexões abertas")
        return
    await websocket.accept()
    connection = Connection(websocket, sub, settings.ws_send_timeout_seconds)
    try:
        await _serve(
            connection, container, hub, principal, _parse_channels(websocket.query_params.get("channels"))
        )
    except WebSocketDisconnect:
        pass
    except Exception as error:
        log.warning("ws_failed", error=type(error).__name__)
    finally:
        hub.unregister(sub)
        await connection.close(CLOSE_TRY_AGAIN)


async def _serve(
    conn: Connection, container: Container, hub: RealtimeHub, principal: Principal, initial: set[ChannelName]
) -> None:
    settings = container.settings
    conn.sub.channels |= initial
    await conn.send(
        frame(
            HelloMessage(
                server_time=container.clock(),
                epoch=hub.epoch,
                channels=list(CHANNELS),
                ping_seconds=settings.ws_ping_seconds,
                runner_connected=hub.runner_connected,
                role="admin" if principal.role == "admin" else "reader",
            )
        )
    )
    await conn.send(hub.notice())
    tasks = [
        asyncio.create_task(_writer(conn)),
        asyncio.create_task(_reader(conn, hub)),
        asyncio.create_task(_keepalive(conn, container, principal)),
    ]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _writer(conn: Connection) -> None:
    sub = conn.sub
    while True:
        text = await sub.queue.get()
        if sub.overflowed.is_set():
            await conn.close(CLOSE_TRY_AGAIN, "cliente lento: reconecte e releia os dados")
            return
        if sub.needs_resync:
            sub.needs_resync = False
            await conn.send(RealtimeHub.resync_frame("Mensagens descartadas por lentidão: releia os dados."))
        if not conn.first_delivery(text):
            continue
        try:
            await conn.send(text)
        except TimeoutError:
            await conn.close(CLOSE_TRY_AGAIN, "cliente lento")
            return


async def _reader(conn: Connection, hub: RealtimeHub) -> None:
    invalid = 0
    window_start = time.monotonic()
    count = 0
    while True:
        raw = await conn.ws.receive()
        if raw["type"] == "websocket.disconnect":
            raise WebSocketDisconnect(raw.get("code", 1000))
        conn.last_seen = time.monotonic()
        payload = raw.get("text")
        if payload is None:
            payload = raw.get("bytes") or b""
        if len(payload) > MAX_CLIENT_MESSAGE:
            await conn.close(CLOSE_TOO_BIG, "mensagem grande demais")
            return
        text = payload if isinstance(payload, str) else payload.decode("utf-8", errors="replace")
        if conn.last_seen - window_start > RATE_WINDOW_SECONDS:
            window_start, count = conn.last_seen, 0
        count += 1
        if count > MAX_MESSAGES_PER_WINDOW:
            await conn.close(CLOSE_POLICY, "mensagens demais")
            return
        try:
            message = _CLIENT.validate_json(text)
        except ValidationError:
            invalid += 1
            await conn.send(frame(ErrorMessage(code="invalid_message", message="Mensagem inválida.")))
            if invalid > MAX_INVALID_MESSAGES:
                await conn.close(CLOSE_POLICY, "mensagens inválidas demais")
                return
            continue
        await _handle(conn, hub, message)


async def _handle(conn: Connection, hub: RealtimeHub, message: Any) -> None:
    if isinstance(message, SubscribeMessage):
        conn.sub.channels |= set(message.channels)
        resumed, frames = hub.replay(message.last_event_id, conn.sub.channels)
        burst = [
            frame(
                SubscribedMessage(channels=sorted(conn.sub.channels), resumed=resumed, replayed=len(frames))
            ),
            *[text for text in frames if conn.first_delivery(text)],
        ]
        if message.last_event_id and not resumed:
            burst.append(RealtimeHub.resync_frame("Não foi possível retomar de onde parou: releia os dados."))
        burst.append(hub.notice())
        await conn.send_burst(burst)
    elif isinstance(message, UnsubscribeMessage):
        conn.sub.channels -= set(message.channels)
        await conn.send(frame(SubscribedMessage(channels=sorted(conn.sub.channels), resumed=False)))
    elif isinstance(message, PongMessage):
        return


async def _keepalive(conn: Connection, container: Container, principal: Principal) -> None:
    settings = container.settings
    last_check = time.monotonic()
    while True:
        await asyncio.sleep(settings.ws_ping_seconds)
        if time.monotonic() - conn.last_seen > settings.ws_ping_seconds * 3:
            await conn.close(CLOSE_POLICY, "sem resposta ao ping")
            return
        if time.monotonic() - last_check >= settings.ws_session_check_seconds:
            last_check = time.monotonic()
            if not await session_alive(container, principal):
                await conn.close(CLOSE_POLICY, "sessão encerrada")
                return
        await conn.send(frame(PingMessage(at=_now(container))))


def _now(container: Container) -> datetime:
    return container.clock()
