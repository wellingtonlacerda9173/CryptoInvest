"""Canal de comandos ASSINADO (Redis) entre a API do painel e o runner.

A API nao tem logica de trading: ela so empurra comandos para a lista Redis `<prefixo>:commands`. Cada
comando viaja num envelope `{"payload": {...}, "sig": "<hmac-sha256 hex>"}` assinado com a chave
compartilhada de arquivo secreto (`command_hmac_key`). O runner so aceita o envelope se:

- o envelope e pequeno e raso, e a assinatura confere (bytes ASCII, tempo constante);
- o carimbo de assinatura `ts` esta dentro de `MAX_SKEW` do relogio local (nem velho, nem do futuro);
- o `id` (nonce) nunca foi visto (anti-replay, em memoria e, quando ha Redis, `SET NX` com TTL);
- o corpo e valido (nome conhecido, operador e motivo nao vazios).

Entrega at-least-once: o `poll` move cada envelope (`LMOVE`) para `<prefixo>:commands:processing` e so o
remove (`commit`/`release`) depois que o comando esta gravado na fila local duravel (ou descartado e
auditado); o nonce so e reivindicado (`SET NX`) nesse momento. Se o runner cair no meio, o envelope reaparece
no proximo `poll` e a gravacao local e idempotente (mesmo nome de arquivo). A confirmacao (`ack`) e assinada
com a mesma chave (dominio `ack`), para a API recusar respostas forjadas por quem escreve no Redis.

A API e o runner usam as mesmas chaves (`RedisKeys`): `<prefixo>:commands` (lista), `...:cmd:nonce:<id>` e
`...:cmd:ack:<id>` (resultado do comando, lido pela API).

O que passa e gravado na `CommandQueue` local do run: dai em diante valem TODAS as protecoes ja existentes
(TTL, descarte de comandos anteriores a partida, `emergency-stop` simples que nunca expira, `liquidate`
30 min, `resume` que nunca reativa HALTED, tentativas e `failed/`). Envelope invalido, velho ou repetido e
DESCARTADO e auditado (`paper_command_discarded`, com limite por minuto), sem nunca conter segredo.
"""

import hashlib
import hmac
import json
import re
import time
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, cast

import structlog
from pydantic import ValidationError
from redis import asyncio as aioredis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from engine.infrastructure.config.logging import scrub_text
from engine.infrastructure.persistence.audit_repository import AuditRepository
from engine.runner.paper_control import Command
from engine.runner.paper_events import RedisKeys
from engine.runner.signing import as_text, canonical, sign_ack, signatures_match, too_deep

log = structlog.get_logger()

KEY_FILE = "command_hmac_key"
MIN_KEY_BYTES = 32
MAX_SKEW = timedelta(seconds=60)
NONCE_TTL_SECONDS = 600
ACK_TTL_SECONDS = 24 * 3600
MAX_ENVELOPE_BYTES = 8192
MAX_QUEUE_LENGTH = 200
WIRE_VERSION = 1
MAX_DEPTH = 8
COMMAND_ID = re.compile(r"[A-Za-z0-9_-]{8,64}")
_IO_ERRORS = (RedisError, OSError, TimeoutError)
_NON_TERMINAL = frozenset({"received", "discarded"})

__all__ = ["canonical"]


class DiscardCode:
    MALFORMED = "malformed"
    TOO_LARGE = "too_large"
    BAD_SIGNATURE = "bad_signature"
    STALE = "stale"
    FUTURE = "future"
    REPLAY = "replay"
    INVALID = "invalid_command"


def load_command_key(secrets_dir: Path | None) -> bytes | None:
    """Chave HMAC compartilhada (arquivo `command_hmac_key`); ausente ou curta = canal assinado desligado."""
    if secrets_dir is None:
        return None
    path = secrets_dir / KEY_FILE
    try:
        key = path.read_text(encoding="utf-8").strip().encode()
    except OSError:
        return None
    if len(key) < MIN_KEY_BYTES:
        log.error("command_key_too_short", minimum=MIN_KEY_BYTES)
        return None
    return key


def _mac(key: bytes, payload: Mapping[str, Any]) -> str:
    return hmac.new(key, canonical(payload), hashlib.sha256).hexdigest()


def command_payload(command: Command, *, command_id: str, signed_at: datetime) -> dict[str, Any]:
    return {
        "v": WIRE_VERSION,
        "id": command_id,
        "ts": signed_at.astimezone(UTC).isoformat(),
        "name": command.name.value,
        "operator": command.operator,
        "reason": command.reason,
        "liquidate": command.liquidate,
        "issued_at": command.issued_at.astimezone(UTC).isoformat(),
        "params": command.params,
    }


def sign_command(key: bytes, command: Command, *, command_id: str, signed_at: datetime) -> str:
    """Envelope pronto para `RPUSH` em `RedisKeys.commands`."""
    payload = command_payload(command, command_id=command_id, signed_at=signed_at)
    return json.dumps({"payload": payload, "sig": _mac(key, payload)}, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class Outcome:
    """Resultado de verificar um envelope: ou `command` (aceito) ou `discarded` com o codigo do motivo."""

    command_id: str | None
    command: Command | None = None
    discarded: str | None = None
    digest: str = ""
    raw: str | bytes = field(default="", repr=False)
    authentic: bool = False
    """Assinatura valida: so entao o `command_id` e confiavel para gravar ack."""


class ReplayGuard:
    """Lembra os nonces ja vistos por `NONCE_TTL_SECONDS` (mais que o dobro da janela de `ts`)."""

    def __init__(self, ttl_seconds: float = NONCE_TTL_SECONDS, max_entries: int = 10_000) -> None:
        self._ttl = ttl_seconds
        self._max = max_entries
        self._seen: OrderedDict[str, float] = OrderedDict()

    def _prune(self, moment: float) -> None:
        while self._seen and (len(self._seen) > self._max or next(iter(self._seen.values())) < moment):
            self._seen.popitem(last=False)

    def seen(self, nonce: str, *, now: float | None = None) -> bool:
        """Consulta sem registrar (o registro so acontece quando o comando ficou duravel)."""
        self._prune(time.monotonic() if now is None else now)
        return nonce in self._seen

    def first_time(self, nonce: str, *, now: float | None = None) -> bool:
        moment = time.monotonic() if now is None else now
        self._prune(moment)
        if nonce in self._seen:
            return False
        self._seen[nonce] = moment + self._ttl
        return True


def verify_envelope(raw: str, key: bytes, now: datetime, guard: ReplayGuard | None = None) -> Outcome:
    """Verificacao TOTAL: qualquer entrada, por pior que seja, vira um `Outcome` (nunca levanta).

    Com `guard`, o nonce e registrado aqui (testes e ferramentas); o `RedisCommandSource` passa `None` e so
    registra depois de gravar o comando na fila local.
    """
    digest = hashlib.sha256(raw.encode(errors="replace")).hexdigest()[:16]
    try:
        return _verify(raw, key, now, guard, digest)
    except Exception:
        return Outcome(None, discarded=DiscardCode.MALFORMED, digest=digest, raw=raw)  # nao autenticado


def _verify(raw: str, key: bytes, now: datetime, guard: ReplayGuard | None, digest: str) -> Outcome:
    def discard(code: str, command_id: str | None = None, *, authentic: bool = True) -> Outcome:
        return Outcome(command_id, discarded=code, digest=digest, raw=raw, authentic=authentic)

    if len(raw.encode(errors="replace")) > MAX_ENVELOPE_BYTES:
        return discard(DiscardCode.TOO_LARGE, authentic=False)
    if too_deep(raw, MAX_DEPTH):
        return discard(DiscardCode.MALFORMED, authentic=False)
    try:
        envelope = json.loads(raw)
        payload = envelope["payload"]
        signature = envelope["sig"]
        if not isinstance(payload, dict) or not isinstance(signature, str):
            raise TypeError("envelope")
        expected = _mac(key, payload)
    except (ValueError, KeyError, TypeError, RecursionError, OverflowError):
        return discard(DiscardCode.MALFORMED, authentic=False)
    raw_id = payload.get("id")
    command_id = raw_id if isinstance(raw_id, str) and COMMAND_ID.fullmatch(raw_id) else None
    if not signatures_match(signature, expected):
        return discard(DiscardCode.BAD_SIGNATURE, command_id, authentic=False)
    try:
        stamp = datetime.fromisoformat(str(payload["ts"]))
        if stamp.tzinfo is None:
            raise ValueError("carimbo sem fuso")
        signed_at = stamp.astimezone(UTC)
    except (KeyError, ValueError, OverflowError):
        return discard(DiscardCode.MALFORMED, command_id)
    if signed_at < now - MAX_SKEW:
        return discard(DiscardCode.STALE, command_id)
    if signed_at > now + MAX_SKEW:
        return discard(DiscardCode.FUTURE, command_id)
    if payload.get("v") != WIRE_VERSION or command_id is None:
        return discard(DiscardCode.INVALID, command_id)
    if guard is not None and not guard.first_time(command_id):
        return discard(DiscardCode.REPLAY, command_id)
    try:
        command = Command.model_validate(
            {k: payload[k] for k in ("name", "operator", "reason", "liquidate", "issued_at", "params")}
        )
    except (ValidationError, KeyError):
        return discard(DiscardCode.INVALID, command_id)
    if command.issued_at > now + MAX_SKEW:
        return discard(DiscardCode.FUTURE, command_id)
    return Outcome(command_id, command=command, digest=digest, raw=raw, authentic=True)


class SignedCommandSource(Protocol):
    async def poll(self, now: datetime) -> list[Outcome]: ...

    async def commit(self, outcome: Outcome) -> bool:
        """O comando de `outcome` ja esta na fila local duravel: reivindica o nonce e tira o envelope da lista
        de processamento. Falso se o nonce ja fora reivindicado (repeticao): o chamador desfaz a gravacao."""
        ...

    async def release(self, outcome: Outcome) -> None:
        """Descarte tratado (auditado): tira o envelope da lista de processamento."""
        ...

    async def ack(self, command_id: str, result: str, detail: str = "") -> None: ...


class RedisCommandSource:
    """Consome `RedisKeys.commands` com entrega at-least-once; erro do Redis nunca derruba o robo."""

    def __init__(
        self, client: "aioredis.Redis", key: bytes, *, batch: int = 20, keys: RedisKeys | None = None
    ) -> None:
        self._redis = client
        self._key = key
        self._batch = batch
        self._keys = keys or RedisKeys()
        self._guard = ReplayGuard()
        self.poll_failures = 0
        pool = getattr(client, "connection_pool", None)
        if getattr(pool, "connection_kwargs", {}).get("decode_responses"):
            raise ValueError(
                "o canal de comandos exige decode_responses=False (veneno nao-UTF-8 cegaria o poll)"
            )

    @classmethod
    def from_url(cls, url: str, key: bytes, *, keys: RedisKeys | None = None) -> "RedisCommandSource":
        client: aioredis.Redis = aioredis.from_url(
            url, socket_connect_timeout=3, socket_timeout=2, decode_responses=False
        )
        return cls(client, key, keys=keys)

    async def poll(self, now: datetime) -> list[Outcome]:
        try:
            await self._claim_batch()
            raw_items = await self._redis.lrange(self._keys.processing, 0, MAX_QUEUE_LENGTH * 2)
        except (*_IO_ERRORS, ValueError) as error:
            self.poll_failures += 1
            log.warning(
                "signed_commands_poll_failed", error=scrub_text(repr(error)), streak=self.poll_failures
            )
            return []
        self.poll_failures = 0
        outcomes: list[Outcome] = []
        in_batch: set[str] = set()
        for item in raw_items or []:
            outcomes.append(self._inspect(item, now, in_batch))
        return outcomes

    def _inspect(self, item: bytes | str, now: datetime, in_batch: set[str]) -> Outcome:
        """Um item nunca afeta os demais; `raw` e o item ORIGINAL (e o que o `LREM` precisa)."""
        try:
            text = as_text(item)
            if text is None:
                digest = hashlib.sha256(bytes(item) if not isinstance(item, str) else b"").hexdigest()[:16]
                return Outcome(None, discarded=DiscardCode.MALFORMED, digest=digest, raw=item)
            outcome = replace(verify_envelope(text, self._key, now), raw=item)
            command_id = outcome.command_id
            if outcome.command is not None and command_id is not None:
                if command_id in in_batch or self._guard.seen(command_id):
                    return replace(outcome, command=None, discarded=DiscardCode.REPLAY)
                in_batch.add(command_id)
            return outcome
        except Exception:
            return Outcome(None, discarded=DiscardCode.MALFORMED, raw=item)

    async def _claim_batch(self) -> None:
        """`commands` -> `processing`; com a fila acima do teto drena ate o teto (o lixo e descartado)."""
        pending = int(await self._redis.llen(self._keys.commands))
        for _ in range(min(pending, MAX_QUEUE_LENGTH if pending > MAX_QUEUE_LENGTH else self._batch)):
            if await self._redis.lmove(self._keys.commands, self._keys.processing, "LEFT", "RIGHT") is None:
                break

    async def commit(self, outcome: Outcome) -> bool:
        command_id = outcome.command_id
        claimed = (
            command_id is not None and self._guard.first_time(command_id) and await self._claim(command_id)
        )
        await self.release(outcome)
        return claimed

    async def release(self, outcome: Outcome) -> None:
        try:
            await self._redis.lrem(self._keys.processing, 1, cast("Any", outcome.raw))
        except _IO_ERRORS as error:
            log.warning("signed_commands_release_failed", error=scrub_text(repr(error)))

    async def _claim(self, command_id: str) -> bool:
        """Anti-replay que sobrevive a reinicio do runner: so o primeiro `SET NX` vence."""
        try:
            return bool(
                await self._redis.set(self._keys.nonce(command_id), "1", nx=True, ex=NONCE_TTL_SECONDS)
            )
        except _IO_ERRORS:
            return True

    async def ack(self, command_id: str, result: str, detail: str = "") -> None:
        body = sign_ack(
            self._key,
            command_id,
            {"result": result, "detail": scrub_text(detail)[:200], "at": _now().isoformat()},
        )
        # monotonico: recebido/descartado so entram se ainda nao ha resultado; o desfecho real sempre vence
        only_new = result in _NON_TERMINAL
        try:
            await self._redis.set(
                self._keys.ack(command_id), json.dumps(body), ex=ACK_TTL_SECONDS, nx=only_new
            )
        except _IO_ERRORS:
            log.warning("signed_command_ack_failed", command_id=command_id)

    async def close(self) -> None:
        try:
            await self._redis.aclose()
        except _IO_ERRORS:
            log.warning("redis_close_failed")


class DiscardLimiter:
    """Evita inundar o `audit_log` e o Redis: ate `per_minute` descartes por minuto sao registrados um a um;
    o excedente so e contado e sai num UNICO registro agregado por minuto (`flush`)."""

    def __init__(self, per_minute: int = 5) -> None:
        self._limit = per_minute
        self._minute: datetime | None = None
        self._admitted = 0
        self._suppressed: dict[str, int] = {}
        self._finished: dict[str, int] = {}

    def _tick(self, now: datetime) -> None:
        minute = now.replace(second=0, microsecond=0)
        if minute == self._minute:
            return
        for code, count in self._suppressed.items():
            self._finished[code] = self._finished.get(code, 0) + count
        self._minute, self._admitted, self._suppressed = minute, 0, {}

    def admit(self, code: str, now: datetime) -> bool:
        self._tick(now)
        if self._admitted < self._limit:
            self._admitted += 1
            return True
        self._suppressed[code] = self._suppressed.get(code, 0) + 1
        return False

    def flush(self, now: datetime) -> dict[str, int] | None:
        """Contagem por codigo dos minutos ja encerrados (entregue uma unica vez), ou `None`."""
        self._tick(now)
        done, self._finished = self._finished, {}
        return done or None


class AuditSink(Protocol):
    async def audit(self, actor: str, action: str, entity: str, payload: Mapping[str, Any]) -> None: ...


class SqlAuditSink:
    """Grava no `audit_log` fora do diario do robo (descartes e config aplicada); melhor esforco."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def audit(self, actor: str, action: str, entity: str, payload: Mapping[str, Any]) -> None:
        try:
            await AuditRepository(self._sessions).append(actor, action, entity, dict(payload))
        except Exception as error:
            log.error("audit_sink_failed", action=action, error=scrub_text(repr(error)))


def _now() -> datetime:
    return datetime.now(UTC)


def discard_payload(outcomes: Sequence[Outcome]) -> list[dict[str, Any]]:
    return [{"command_id": o.command_id, "code": o.discarded, "digest": o.digest} for o in outcomes]
