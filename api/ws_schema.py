"""Contrato das mensagens do WebSocket `/ws` (exportado em `api/ws_schema.json`).

Fluxo: o painel abre `/ws` (cookie de sessão + `Origin` do painel), recebe `hello`, envia `subscribe` com os
canais e, opcionalmente, o `last_event_id` da última mensagem recebida (retomada sem perda quando possível).
O servidor envia `ping` periodicamente; o cliente responde `pong`. Mensagens `event` trazem o `id` a guardar.
"""

import json
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

ChannelName = Literal["status", "decisions", "orders", "trades", "equity", "alerts", "heartbeat"]
CHANNELS: tuple[ChannelName, ...] = (
    "status",
    "decisions",
    "orders",
    "trades",
    "equity",
    "alerts",
    "heartbeat",
)
# canal do painel <- tipo publicado pelo runner (`state` e `risk` mudam de nome; o resto é igual)
RUNNER_TO_CHANNEL: dict[str, ChannelName] = {
    "state": "status",
    "risk": "alerts",
    "alerts": "alerts",
    "decisions": "decisions",
    "orders": "orders",
    "trades": "trades",
    "equity": "equity",
    "heartbeat": "heartbeat",
}
CLOSE_POLICY = 1008
CLOSE_TOO_BIG = 1009
CLOSE_TRY_AGAIN = 1013


class _Msg(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SubscribeMessage(_Msg):
    type: Literal["subscribe"]
    channels: list[ChannelName] = Field(min_length=1, max_length=len(CHANNELS))
    last_event_id: str | None = Field(
        default=None, max_length=40, description="`id` do último `event` recebido; permite retomar."
    )


class UnsubscribeMessage(_Msg):
    type: Literal["unsubscribe"]
    channels: list[ChannelName] = Field(min_length=1, max_length=len(CHANNELS))


class PongMessage(_Msg):
    type: Literal["pong"]


ClientMessage = Annotated[SubscribeMessage | UnsubscribeMessage | PongMessage, Field(discriminator="type")]


class HelloMessage(_Msg):
    type: Literal["hello"] = "hello"
    server_time: datetime
    epoch: str = Field(description="Muda quando a API reinicia: `last_event_id` de outra época não retoma.")
    channels: list[ChannelName]
    ping_seconds: float
    runner_connected: bool
    role: Literal["admin", "reader"]


class SubscribedMessage(_Msg):
    type: Literal["subscribed"] = "subscribed"
    channels: list[ChannelName]
    resumed: bool = Field(description="Verdadeiro: as mensagens perdidas foram reenviadas.")
    replayed: int = 0


class EventMessage(_Msg):
    type: Literal["event"] = "event"
    id: str = Field(examples=["a1b2c3d4:1042"])
    channel: ChannelName
    at: datetime
    data: dict[str, Any]


class RunnerMessage(_Msg):
    type: Literal["runner"] = "runner"
    connected: bool
    message: str = Field(examples=["Desconectado do robô: sem sinal há mais de 90 segundos."])
    last_heartbeat_at: datetime | None = None


class ResyncMessage(_Msg):
    type: Literal["resync"] = "resync"
    reason: str


class PingMessage(_Msg):
    type: Literal["ping"] = "ping"
    at: datetime


class ErrorMessage(_Msg):
    type: Literal["error"] = "error"
    code: str
    message: str


ServerMessage = Annotated[
    HelloMessage
    | SubscribedMessage
    | EventMessage
    | RunnerMessage
    | ResyncMessage
    | PingMessage
    | ErrorMessage,
    Field(discriminator="type"),
]


def build_ws_schema_text() -> str:
    """JSON Schema determinístico dos dois sentidos, com a ordem do fluxo na descrição."""
    server = TypeAdapter(ServerMessage).json_schema(ref_template="#/$defs/{model}")
    client = TypeAdapter(ClientMessage).json_schema(ref_template="#/$defs/{model}")
    defs = {**server.pop("$defs", {}), **client.pop("$defs", {})}
    document = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "CryptoInvest WebSocket /ws",
        "description": (__doc__ or "").strip(),
        "channels": list(CHANNELS),
        "serverMessage": server,
        "clientMessage": client,
        "$defs": defs,
    }
    return json.dumps(document, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
