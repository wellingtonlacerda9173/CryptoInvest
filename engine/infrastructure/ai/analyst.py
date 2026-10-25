"""Analista de IA com degradacao controlada: qualquer problema vira "sem parecer", nunca excecao.

Ordem de cada pedido: cache -> circuito -> orcamento/limite -> chamada (timeout, no maximo 2 tentativas) ->
validacao estrita -> auditoria. O gasto do mes e lido do registro append-only a CADA chamada (o teto vale
entre processos que compartilham o livro) e, quando o livro suporta, o custo de pior caso e RESERVADO
por INSERT atomico antes da chamada. A parada e DURA: se a estimativa pessimista estoura o teto, nao ha
chamada. Chamada cancelada no meio (timeout externo, shutdown) vira 'cancelled' com o custo de pior caso.
"""

import asyncio
import contextlib
import hashlib
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import TypeVar

import structlog

from engine.application.ai import PROMPT_VERSION, AnalystOpinion, AnalystRequest
from engine.application.ai.ledger import (
    MAX_RESPONSE_CHARS,
    AiCallRecord,
    AiCallStore,
    CallResult,
    ReservingStore,
    month_start,
)
from engine.application.ai.parse import parse_opinion
from engine.application.ai.sanitize import sanitize_text
from engine.infrastructure.ai.client import MAX_OUTPUT_TOKENS, LlmClient, LlmError, LlmReply, LlmTimeoutError
from engine.infrastructure.ai.guards import CircuitBreaker, HourlyLimit, ReplyCache
from engine.infrastructure.ai.pricing import DEFAULT_PRICES, Price, estimate_cost, estimate_input_tokens
from engine.infrastructure.ai.prompt import system_prompt, user_message
from engine.infrastructure.config.logging import scrub_text

log = structlog.get_logger()

T = TypeVar("T")

MAX_ATTEMPTS = 2
BACKOFF_SECONDS = 1.0


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class AnthropicAnalyst:
    def __init__(
        self,
        client: LlmClient,
        store: AiCallStore,
        *,
        model: str,
        monthly_budget_usd: Decimal,
        timeout_seconds: float,
        max_calls_per_hour: int = 30,
        prices: Mapping[str, Price] = DEFAULT_PRICES,
        clock: Callable[[], datetime] = _utc_now,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        circuit: CircuitBreaker | None = None,
    ) -> None:
        self._client = client
        self._store = store
        self._model = model
        self._budget = monthly_budget_usd
        self._timeout = timeout_seconds
        self._prices = prices
        self._clock = clock
        self._sleep = sleep
        self._circuit = circuit or CircuitBreaker()
        self._hourly = HourlyLimit(max_calls_per_hour)
        self._cache = ReplyCache()
        self._spent = Decimal(0)
        self._lock = asyncio.Lock()
        self._pending_records: set[asyncio.Future[None]] = set()

    async def analyze(self, request: AnalystRequest, *, decision_key: str = "") -> AnalystOpinion | None:
        try:
            async with self._lock:
                return await self._analyze(request, decision_key)
        except Exception as error:  # a IA nunca derruba o runner
            log.error("ai_analyst_failed", error=scrub_text(repr(error)))
            return None

    def status(self) -> dict[str, object]:
        now = self._clock()
        return {
            "circuit_open": self._circuit.is_open,
            "consecutive_failures": self._circuit.failures,
            "spent_month_usd": str(self._spent),
            "budget_usd": str(self._budget),
            "calls_last_hour": self._hourly.used(now),
            "model": self._model,
            "client_notes": dict(getattr(self._client, "notes", {})),
        }

    async def _read_spent(self, now: datetime) -> bool:
        try:
            usage = await self._store.usage(month_start(now))
        except Exception as error:  # sem o contador do mes nao ha como garantir o teto: sem chamada
            log.error("ai_usage_unavailable", error=scrub_text(repr(error)))
            return False
        self._spent = usage.spent_usd
        return True

    async def complete(
        self,
        system: str,
        user: str,
        *,
        prompt_version: str,
        parse: Callable[[str], T | None],
        decision_key: str = "",
    ) -> T | None:
        """Mesma estrada do parecer (circuito, orcamento DURO, limite por hora, timeout, auditoria), para
        textos que nao sao pareceres (ex.: relatorio de licoes). Nunca levanta; None = sem resposta valida."""
        try:
            async with self._lock:
                digest = hashlib.sha256(f"{system}|{user}".encode()).hexdigest()
                return await self._guarded(digest, system, user, prompt_version, parse, decision_key)
        except Exception as error:
            log.error("ai_complete_failed", error=scrub_text(repr(error)))
            return None

    async def _analyze(self, request: AnalystRequest, decision_key: str) -> AnalystOpinion | None:
        digest = request.digest()
        if (cached := self._cache.get(digest)) is not None:
            return cached
        opinion = await self._guarded(
            digest,
            system_prompt(request),
            user_message(request),
            PROMPT_VERSION,
            lambda raw: parse_opinion(raw, self._model, PROMPT_VERSION),
            decision_key,
        )
        if opinion is not None:
            self._cache.put(digest, opinion)
        return opinion

    async def _guarded(
        self,
        digest: str,
        system: str,
        user: str,
        version: str,
        parse: Callable[[str], T | None],
        decision_key: str,
    ) -> T | None:
        now = self._clock()
        if not self._circuit.allows(now):
            return None
        if not await self._read_spent(now):
            return None
        reserve_in = estimate_input_tokens(system, user)
        worst = estimate_cost(self._model, reserve_in, MAX_OUTPUT_TOKENS, self._prices)
        over = self._spent + worst > self._budget
        reservation: int | None = None
        if not over and self._hourly.allows(now) and isinstance(self._store, ReservingStore):
            try:
                reservation = await self._store.reserve(
                    self._reserve_record(now, digest, version, worst, decision_key),
                    budget=self._budget,
                    month_start=month_start(now),
                )
            except Exception as error:  # sem reserva nao ha como garantir o teto: sem chamada
                log.error("ai_reserve_failed", error=scrub_text(repr(error)))
                return None
            over = reservation is None
        if not self._hourly.allows(now) or over:
            await self._record(now, digest, "budget", version, decision_key=decision_key)
            return None
        self._hourly.hit(now)
        started = self._clock()
        try:
            reply, failure = await self._call(system, user)
        except BaseException:
            # cancelada no meio: a chamada pode ja ter sido enviada (e cobrada); registra sem engolir
            await self._settle_shielded(
                now, digest, "cancelled", version, reservation, cost=worst, decision_key=decision_key
            )
            raise
        latency = int((self._clock() - started).total_seconds() * 1000)
        if reply is None:
            cost = (
                estimate_cost(self._model, reserve_in, 0, self._prices)
                if failure == "timeout"
                else Decimal(0)
            )
            self._circuit.failure(self._clock())
            await self._record(
                now,
                digest,
                "timeout" if failure == "timeout" else "error",
                version,
                reservation,
                cost=cost,
                latency=latency,
                decision_key=decision_key,
            )
            return None
        cost = estimate_cost(self._model, reply.input_tokens, reply.output_tokens, self._prices)
        parsed = parse(reply.text)
        if parsed is None:
            self._circuit.failure(self._clock())
        else:
            self._circuit.success()
        await self._record(
            now,
            digest,
            "ok" if parsed is not None else "invalid",
            version,
            reservation,
            reply=reply,
            cost=cost,
            latency=latency,
            decision_key=decision_key,
        )
        return parsed

    async def _call(self, system: str, user: str) -> tuple[LlmReply | None, str]:
        failure = "error"
        for attempt in range(MAX_ATTEMPTS):
            try:
                reply = await asyncio.wait_for(
                    self._client.complete(
                        model=self._model,
                        system=system,
                        user=user,
                        max_tokens=MAX_OUTPUT_TOKENS,
                        deadline_seconds=self._timeout,
                    ),
                    timeout=self._timeout + 1,
                )
                return reply, ""
            except TimeoutError:
                failure, retryable = "timeout", True
            except LlmError as error:
                failure = "timeout" if isinstance(error, LlmTimeoutError) else "error"
                retryable = error.retryable
            if not retryable or attempt + 1 == MAX_ATTEMPTS:
                break
            await self._sleep(BACKOFF_SECONDS * (attempt + 1))
        return None, failure

    def _reserve_record(
        self, now: datetime, digest: str, version: str, worst: Decimal, decision_key: str
    ) -> AiCallRecord:
        return AiCallRecord(
            called_at=now,
            model=self._model,
            prompt_version=version,
            prompt_hash=digest,
            cost_usd=worst.quantize(Decimal("0.000001")),
            result="reserve",
            decision_key=decision_key[:64] or None,
        )

    async def _settle_shielded(
        self,
        now: datetime,
        digest: str,
        result: CallResult,
        version: str,
        reservation: int | None,
        *,
        cost: Decimal,
        decision_key: str,
    ) -> None:
        task = asyncio.ensure_future(
            self._record(now, digest, result, version, reservation, cost=cost, decision_key=decision_key)
        )
        self._pending_records.add(task)
        task.add_done_callback(self._pending_records.discard)
        with contextlib.suppress(asyncio.CancelledError):  # 2a interrupcao: o registro segue em 2o plano
            await asyncio.shield(task)

    async def _record(
        self,
        now: datetime,
        digest: str,
        result: CallResult,
        version: str,
        reservation: int | None = None,
        *,
        reply: LlmReply | None = None,
        cost: Decimal = Decimal(0),
        latency: int = 0,
        decision_key: str = "",
    ) -> None:
        self._spent += cost
        call = AiCallRecord(
            called_at=now,
            model=self._model,
            prompt_version=version,
            prompt_hash=digest,
            response_hash=None if reply is None else _sha(reply.text),
            response_text=None if reply is None else sanitize_text(reply.text, MAX_RESPONSE_CHARS),
            input_tokens=0 if reply is None else reply.input_tokens,
            output_tokens=0 if reply is None else reply.output_tokens,
            cost_usd=cost.quantize(Decimal("0.000001")),
            latency_ms=latency,
            result=result,
            decision_key=decision_key[:64] or None,
        )
        try:
            if reservation is not None and isinstance(self._store, ReservingStore):
                await self._store.settle(call, reservation)
            else:
                await self._store.record(call)
        except Exception as error:  # o gasto ja esta somado em memoria; a auditoria falhou
            log.error("ai_call_audit_failed", error=scrub_text(repr(error)), result=result)
