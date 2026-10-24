"""Comandos do painel ao runner: fila durável no banco + entrega assinada (Redis) com arquivo como reserva.

A API não tem lógica de trading: cada comando vira uma linha em `control_commands` (estado visível) e é
entregue assim que o runner dá sinal de vida. Sem conexão o comando FICA na fila ("pendente de entrega") até
o TTL; a parada de emergência simples nunca expira e é reenviada até ser confirmada.
"""

import asyncio
import uuid
from datetime import datetime, timedelta
from typing import Any, cast

import structlog
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.audit import AuditAction, AuditResult, AuditService, RequestMeta
from api.container import Container
from api.cursors import decode_cursor, encode_cursor
from api.errors import NotFound
from api.schemas_control import CommandAccepted, CommandKind, CommandOut, CommandPage, CommandState
from api.services.runner_status import RunnerSnapshot, RunnerStatusService
from engine.infrastructure.persistence.models import ControlCommandRow
from engine.runner.command_channel import sign_command
from engine.runner.paper_config import RunPaths
from engine.runner.paper_control import (
    COMMAND_TTL,
    LIQUIDATE_TTL,
    Command,
    CommandName,
    CommandQueue,
)

log = structlog.get_logger()

SYSTEM_ACTOR = "painel"
ACK_GRACE_SECONDS = 90.0
EXECUTED = frozenset({"applied", "partially_applied", "refused", "failed"})
# descartes por relógio (carimbo velho/do futuro): a parada de emergência é reenviada com NOVO id
CLOCK_DISCARDS = frozenset({"stale", "future"})
RESEND_BACKOFF_SECONDS = 5.0
RESEND_BACKOFF_CAP_SECONDS = 60.0
# comando por arquivo cujo run morreu: vence `expires_at` + folga (o runner nem chega a descartar o arquivo)
FILE_EXPIRY_GRACE_SECONDS = 60.0
# parada de emergencia: uma tentativa pendente por pedido; `send_ids` guarda so as ultimas
MAX_SEND_IDS = 5
EMERGENCY_ALERT_ATTEMPTS = 5

STATE_TEXT: dict[str, str] = {
    "pending_delivery": "Pendente de entrega: o robô está sem conexão com o painel. O comando fica na fila "
    "e será entregue assim que o robô voltar.",
    "delivered": "Entregue ao robô, aguardando a confirmação.",
    "applied": "Aplicado pelo robô.",
    "partially_applied": "Aplicado em parte pelo robô: o que aumenta o risco só vale no próximo início.",
    "refused": "Recusado pelo robô: nada mudou.",
    "discarded": "Descartado pelo robô por segurança (assinatura, validade ou repetição).",
    "expired": "Expirou antes de ser entregue ou confirmado. Nada mudou: refaça o pedido se ainda precisar.",
    "failed": "O robô recebeu o comando, mas não conseguiu aplicar. Nada mudou.",
}
TERMINAL = frozenset({"applied", "partially_applied", "refused", "discarded", "expired", "failed"})


def command_ttl(name: CommandName, liquidate: bool) -> timedelta | None:
    """Parada de emergência simples nunca expira (só reduz risco); vender tudo vale 30 min; o resto, 10."""
    if name is CommandName.EMERGENCY_STOP:
        return LIQUIDATE_TTL if liquidate else None
    return COMMAND_TTL


def operator_label(username: str, display_name: str | None) -> str:
    if display_name and display_name.strip().lower() != username.lower():
        return f"{display_name.strip()} ({username})"[:100]
    return username[:100]


def to_out(row: ControlCommandRow) -> CommandOut:
    result = row.result or {}
    text = STATE_TEXT[row.state]
    if row.state == "pending_delivery" and result.get("waiting"):
        text = f"{text} Motivo da espera: {result['waiting']}"
    if row.state == "delivered" and result.get("stage") == "received":
        text = "Recebido pelo robô, aplicando."
    if row.state in {"partially_applied", "refused"} and result.get("detail"):
        text = f"{text} {result['detail']}"
    return CommandOut(
        id=row.id,
        name=cast("CommandKind", row.name),
        operator=row.operator,
        reason=row.reason,
        liquidate=row.liquidate,
        state=cast("CommandState", row.state),
        state_text=text,
        channel=cast("Any", row.channel),
        created_at=row.created_at,
        expires_at=row.expires_at,
        delivered_at=row.delivered_at,
        resolved_at=row.resolved_at,
        detail=result.get("detail"),
    )


def accepted_message(row: ControlCommandRow) -> str:
    if row.state == "pending_delivery":
        return (
            "O robô está sem conexão com o painel. O comando ficou na fila e será entregue assim que ele "
            "voltar. Acompanhe o estado em Controle."
        )
    if row.state in ("expired", "failed"):
        return STATE_TEXT[row.state]
    return "Comando enviado ao robô."


def _command_of(row: ControlCommandRow) -> Command:
    return Command(
        name=CommandName(row.name),
        operator=row.operator,
        reason=row.reason,
        liquidate=row.liquidate,
        issued_at=row.created_at,
        params=dict(row.params or {}),
    )


def _parse_time(raw: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(raw)) if raw else None
    except ValueError:
        return None


class CommandService:
    def __init__(self, container: Container, audit: AuditService | None = None) -> None:
        self._c = container
        self._audit = audit or AuditService(container.sessions)

    # ------------------------------------------------------------------ pedido

    async def submit(
        self,
        *,
        name: CommandName,
        operator: str,
        reason: str,
        actor: str,
        meta: RequestMeta,
        liquidate: bool = False,
        params: dict[str, Any] | None = None,
    ) -> CommandAccepted:
        now = self._c.clock()
        ttl = command_ttl(name, liquidate)
        row = ControlCommandRow(
            id=uuid.uuid4().hex,
            name=name.value,
            operator=operator[:100],
            reason=reason,
            liquidate=liquidate,
            params=params or {},
            state="pending_delivery",
            channel=None,
            created_at=now,
            expires_at=None if ttl is None else now + ttl,
        )
        async with self._c.sessions.begin() as db:
            db.add(row)
            await self._audit.record(
                actor=actor,
                action=AuditAction.CONTROL_COMMAND,
                target=f"command:{row.id}",
                result=AuditResult.SUCCESS,
                meta=meta,
                details={
                    "command": name.value,
                    "operator": row.operator,
                    "reason": reason,
                    "liquidate": liquidate,
                    "fields": sorted((params or {}).get("risk", {}))
                    if name is CommandName.APPLY_CONFIG
                    else None,
                },
                session=db,
            )
        await self.deliver(row.id)
        fresh = await self.get_row(row.id)
        return CommandAccepted(**to_out(fresh).model_dump(), message=accepted_message(fresh))

    # ------------------------------------------------------------------ entrega

    async def dispatch_pending(self) -> int:
        async with self._c.sessions() as db:
            stmt = (
                select(ControlCommandRow.id)
                .where(ControlCommandRow.state == "pending_delivery")
                .order_by((ControlCommandRow.name != "emergency-stop"), ControlCommandRow.created_at)
                .limit(50)
            )
            ids = list((await db.scalars(stmt)).all())
        for command_id in ids:
            await self.deliver(command_id)
        return len(ids)

    async def deliver(self, command_id: str) -> None:
        snapshot = await RunnerStatusService(self._c).snapshot()
        async with self._c.sessions.begin() as db:
            row = await db.get(ControlCommandRow, command_id, with_for_update={"skip_locked": True})
            if row is None or row.state != "pending_delivery":
                return
            now = self._c.clock()
            if row.expires_at is not None and now >= row.expires_at:
                self._resolve(row, "expired", now, "Venceu antes de o robô voltar.")
                await self._record_resolution(row, db)
                return
            retry_after = _parse_time((row.result or {}).get("retry_after"))
            if retry_after is not None and now < retry_after:
                return
            waiting = self._waiting_reason(row, snapshot)
            if waiting is not None:
                if waiting.startswith("EXPIRE:"):
                    self._resolve(row, "expired", now, waiting.removeprefix("EXPIRE:"))
                    await self._record_resolution(row, db)
                elif (row.result or {}).get("waiting") != waiting:
                    row.result = {**(row.result or {}), "waiting": waiting}
                return
            attempts = int((row.result or {}).get("attempts", 0)) + 1
            send_id = row.id if attempts == 1 else f"{row.id}-{attempts}"
            channel = await self._send(row, snapshot, now, send_id)
            if channel is None:
                no_key = self._c.command_key is None and self._c.settings.runner_data_dir is None
                waiting = (
                    "a chave de assinatura dos comandos (command_hmac_key) não está configurada"
                    if no_key
                    else "o canal de comandos não aceitou a entrega"
                )
                if (row.result or {}).get("waiting") != waiting:
                    row.result = {**(row.result or {}), "waiting": waiting}
                return
            previous = (row.result or {}).get("send_ids") or []
            row.state = "delivered"
            row.channel = channel
            row.delivered_at = now
            row.result = {
                "attempts": attempts,
                "stage": "sent",
                "send_ids": [*previous, send_id][-MAX_SEND_IDS:],
                "run_id": snapshot.run_id,
                "file": CommandQueue.file_name(_command_of(row)),
            }
            await self._record_resolution(row, db)

    def _waiting_reason(self, row: ControlCommandRow, snapshot: RunnerSnapshot) -> str | None:
        if not snapshot.alive:
            if snapshot.connection == "unknown":
                return "sem conexão com o canal de comandos (Redis)"
            return "o robô não está enviando sinal de vida"
        if snapshot.state in {"STOPPED", "STOPPING"}:
            return "o robô está parado ou encerrando"
        started = snapshot.started_at
        if row.name != CommandName.EMERGENCY_STOP.value and started is not None and started > row.created_at:
            return "EXPIRE:O robô reiniciou depois do pedido; comandos antigos são descartados por segurança."
        return None

    async def _send(
        self, row: ControlCommandRow, snapshot: RunnerSnapshot, now: datetime, send_id: str
    ) -> str | None:
        command = _command_of(row)
        c = self._c
        if c.link is not None and c.command_key is not None:
            envelope = sign_command(c.command_key, command, command_id=send_id, signed_at=now)
            if await c.link.push(envelope):
                return "redis"
        data_dir = c.settings.runner_data_dir
        if data_dir is not None and snapshot.run_id is not None:
            try:
                paths = RunPaths(data_dir, snapshot.run_id)
                if paths.root.is_dir():
                    await asyncio.to_thread(CommandQueue(paths.commands).send, command)
                    return "file"
            except (OSError, ValueError):
                log.warning("command_file_fallback_failed")
        return None

    # ------------------------------------------------------------------ confirmação

    async def reconcile(self) -> int:
        """Lê as confirmações do runner, reenvia a parada de emergência sem resposta e vence o que expirou."""
        changed = 0
        async with self._c.sessions.begin() as db:
            stmt = (
                select(ControlCommandRow)
                .where(
                    or_(
                        ControlCommandRow.state == "delivered",
                        (ControlCommandRow.state == "pending_delivery")
                        & ControlCommandRow.expires_at.is_not(None),
                    )
                )
                .order_by(ControlCommandRow.created_at)
                .with_for_update(skip_locked=True)
                .limit(100)
            )
            for row in (await db.scalars(stmt)).all():
                changed += await self._reconcile_row(row, db)
        return changed

    async def _reconcile_row(self, row: ControlCommandRow, db: AsyncSession) -> int:
        now = self._c.clock()
        if row.state == "pending_delivery":
            if row.expires_at is not None and now >= row.expires_at:
                self._resolve(row, "expired", now, "Venceu antes de o robô voltar.")
                await self._record_resolution(row, db)
                return 1
            return 0
        verdict = await self._verdict(row)
        if verdict is not None:
            outcome, detail = verdict
            if outcome == "received":
                row.result = {**(row.result or {}), "stage": "received"}
                return 0
            if outcome == "discarded" and self._must_resend(row, detail):
                waiting = f"o robô descartou o carimbo ({detail}); reenviando com novo carimbo"
                await self._withdraw_last(row)
                self._requeue(row, now, waiting, backoff=True)
                log.error("emergency_stop_resent", command=row.id, discarded_as=detail)
                return 1
            if outcome in TERMINAL:
                self._resolve(row, outcome, now, detail or None)
                await self._record_resolution(row, db)
                return 1
        if row.delivered_at is not None and (now - row.delivered_at).total_seconds() > ACK_GRACE_SECONDS:
            if row.name == CommandName.EMERGENCY_STOP.value:
                await self._withdraw_last(row)
                attempts = int((row.result or {}).get("attempts", 1))
                waiting = "sem confirmação do robô; reenviando"
                if attempts >= EMERGENCY_ALERT_ATTEMPTS:
                    waiting = f"sem confirmação do robô após {attempts} tentativas; seguimos reenviando"
                    if attempts == EMERGENCY_ALERT_ATTEMPTS:
                        log.error("emergency_stop_unconfirmed", command=row.id, attempts=attempts)
                self._requeue(row, now, waiting, backoff=attempts >= EMERGENCY_ALERT_ATTEMPTS)
                return 1
            if row.channel == "file":
                limit = (
                    None
                    if row.expires_at is None
                    else row.expires_at + timedelta(seconds=FILE_EXPIRY_GRACE_SECONDS)
                )
                if limit is not None and now >= limit:
                    self._resolve(row, "expired", now, "O robô parou antes de consumir o comando.")
                    await self._record_resolution(row, db)
                    return 1
                return 0
            self._resolve(row, "expired", now, "O robô não confirmou o recebimento.")
            await self._record_resolution(row, db)
            return 1
        return 0

    async def _withdraw_last(self, row: ControlCommandRow) -> None:
        """Tira da fila do Redis a tentativa anterior (se o robô ainda não a pegou): 1 pendente por pedido."""
        send_ids = (row.result or {}).get("send_ids") or []
        if row.channel == "redis" and self._c.link is not None and send_ids:
            await self._c.link.withdraw(str(send_ids[-1]))

    @staticmethod
    def _must_resend(row: ControlCommandRow, detail: str) -> bool:
        return row.name == CommandName.EMERGENCY_STOP.value and detail in CLOCK_DISCARDS

    @staticmethod
    def _requeue(row: ControlCommandRow, now: datetime, waiting: str, *, backoff: bool = False) -> None:
        """Volta para a fila com NOVO id/carimbo na próxima entrega (o pedido lógico é o mesmo: o runner trata
        parada repetida como no-op). Com `backoff`, espera crescente entre tentativas (descartes em
        sequência indicam relógio errado: não inundar o canal)."""
        attempts = int((row.result or {}).get("attempts", 1))
        pause = min(RESEND_BACKOFF_SECONDS * attempts, RESEND_BACKOFF_CAP_SECONDS) if backoff else 0.0
        row.state = "pending_delivery"
        row.channel = None
        row.result = {
            **(row.result or {}),
            "waiting": waiting,
            "retry_after": (now + timedelta(seconds=pause)).isoformat(),
        }

    async def _verdict(self, row: ControlCommandRow) -> tuple[str, str] | None:
        """`(resultado, detalhe)` que o runner deu ao comando: confirmação assinada (Redis) ou o arquivo
        arquivado (processed/failed/expired) do canal de arquivo."""
        result = row.result or {}
        if row.channel == "redis" and self._c.link is not None:
            ids = list(result.get("send_ids") or [row.id])
            acks = [(await self._c.link.ack(send_id)) for send_id in reversed(ids)]
            decisive = next((a for a in acks if a and a.get("result") in EXECUTED), None)
            chosen = decisive or (acks[0] if acks else None)
            return None if chosen is None else (str(chosen.get("result")), str(chosen.get("detail") or ""))
        data_dir = self._c.settings.runner_data_dir
        run_id, name = result.get("run_id"), result.get("file")
        if row.channel == "file" and data_dir is not None and run_id and name:
            try:
                queue = CommandQueue(RunPaths(data_dir, str(run_id)).commands)
                return await asyncio.to_thread(queue.outcome_of, str(name))
            except (OSError, ValueError):
                return None
        return None

    def _resolve(self, row: ControlCommandRow, state: str, now: datetime, detail: str | None) -> None:
        row.state = state
        row.resolved_at = now
        row.result = {**(row.result or {}), "detail": detail} if detail else dict(row.result or {})

    async def _record_resolution(self, row: ControlCommandRow, db: AsyncSession) -> None:
        await self._audit.record(
            actor=SYSTEM_ACTOR,
            action=AuditAction.CONTROL_COMMAND_RESOLVED,
            target=f"command:{row.id}",
            result=AuditResult.FAILURE
            if row.state in {"failed", "discarded", "expired", "refused"}
            else AuditResult.SUCCESS,
            meta=RequestMeta(ip=None, user_agent="painel", request_id=None),
            details={
                "command": row.name,
                "state": row.state,
                "channel": row.channel,
                "detail": (row.result or {}).get("detail"),
            },
            session=db,
        )

    # ------------------------------------------------------------------ leitura

    async def get_row(self, command_id: str) -> ControlCommandRow:
        async with self._c.sessions() as db:
            row = await db.get(ControlCommandRow, command_id)
        if row is None:
            raise NotFound("Comando não encontrado.")
        return row

    async def get(self, command_id: str) -> CommandOut:
        return to_out(await self.get_row(command_id))

    async def list(self, *, limit: int, cursor: str | None) -> CommandPage:
        stmt = select(ControlCommandRow).order_by(
            ControlCommandRow.created_at.desc(), ControlCommandRow.id.desc()
        )
        if cursor:
            after = decode_cursor(cursor)
            moment, last_id = after.moment, after.last_id
            stmt = stmt.where(
                (ControlCommandRow.created_at < moment)
                | ((ControlCommandRow.created_at == moment) & (ControlCommandRow.id < last_id))
            )
        async with self._c.sessions() as db:
            rows = list((await db.scalars(stmt.limit(limit + 1))).all())
        page = rows[:limit]
        nxt = encode_cursor(page[-1].created_at, page[-1].id) if len(rows) > limit else None
        return CommandPage(items=[to_out(r) for r in page], next_cursor=nxt)


class CommandPump:
    """Tarefa de fundo da API: entrega o que ficou pendente e lê as confirmações do runner."""

    def __init__(self, container: Container) -> None:
        self._c = container

    async def run(self) -> None:
        service = CommandService(self._c)
        while True:
            try:
                await service.dispatch_pending()
                await service.reconcile()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log.error("command_pump_failed", error=type(error).__name__)
            await asyncio.sleep(self._c.settings.command_pump_seconds)
