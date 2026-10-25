"""Trava de ativacao do Live: verifica TUDO no servidor e so entao grava uma AUTORIZACAO append-only.

Gravar a autorizacao nao liga nada: o runner Live (outra etapa) so a LE. Toda recusa e auditada com o
motivo exato (`Unmet.code`).
"""

import uuid
from datetime import datetime, timedelta
from typing import Any

import structlog
from pydantic_core import to_jsonable_python
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.audit import AuditAction, AuditResult, AuditService, RequestMeta
from api.binance_keys import KeyPermissions
from api.container import Container
from api.errors import ApiError, Conflict, FieldError, NotFound
from api.persistence.models import ExchangeCredentialRow
from api.schemas_control import (
    LiveActivationRequest,
    LiveAuthorizationOut,
    LiveAuthorizationStateOut,
)
from api.services.credentials import CredentialService
from api.services.livegate import LiveGateService, criteria_of
from api.services.queries import latest_run
from api.services.sessions import Principal
from api.services.status import current_run_id
from engine.application.live_authorization import (
    LIMIT_INCREASE_DAYS,
    LIVE_PHRASE,
    REDUCED_CAPITAL_DAYS,
    ActivationInputs,
    ActivationPolicy,
    KeyFacts,
    Unmet,
    authorization_mac,
    authorization_mac_valid,
    authorization_record,
    evaluate_activation,
    live_stage,
)
from engine.infrastructure.persistence.models import (
    LiveAuthorizationRevocationRow,
    LiveAuthorizationRow,
    RunRow,
)
from engine.runner.paper_report_data import UnknownRunError, collect_inputs

log = structlog.get_logger()
_LOCK_KEY = 7_150_001
REFUSED_DETAIL = "Live nao autorizado: ha criterios nao atendidos."


class LiveActivationService:
    def __init__(self, container: Container, audit: AuditService) -> None:
        self._c = container
        self._audit = audit

    # ---- ativacao ---------------------------------------------------------------------------------

    async def activate(
        self, *, principal: Principal, body: LiveActivationRequest, meta: RequestMeta
    ) -> LiveAuthorizationOut:
        c = self._c
        if self._mac_key() is None:
            raise Conflict("Live indisponivel: chave de assinatura das autorizacoes nao configurada.")
        now = c.clock()
        run_id = body.run_id or await current_run_id(c)
        paper_run_ok = await self._is_paper_run(run_id)
        evaluation = await LiveGateService(c).evaluate(run_id if paper_run_ok else None, fresh=True)
        credential = await self._credential(body.credential_id, principal, meta)
        inputs = ActivationInputs(
            now=now,
            run_id=run_id if paper_run_ok else None,
            no_test=evaluation.no_test,
            eligible=evaluation.eligible,
            gate_approved=evaluation.report.approved,
            metrics=evaluation.report.metrics,
            daily_loss_limit=evaluation.thresholds.max_daily_loss,
            phrase=body.phrase,
            confirm_real_money=body.confirm_real_money,
            initial_capital=body.initial_capital_quote,
            key=_key_facts(credential),
            live_run_active=await self._live_run_active(),
            active_authorization=await self._active_authorization(now) is not None,
        )
        unmet = evaluate_activation(inputs, self._policy())
        if unmet or credential is None or run_id is None or body.reason is None:
            if body.reason is None:
                unmet = (*unmet, Unmet(code="reason", message="Informe o motivo da ativacao."))
            await self._refuse(principal, meta, unmet, inputs, credential_id=body.credential_id)
            raise _refusal(unmet)
        if body.initial_capital_quote is None:
            raise _refusal(unmet)
        criteria = {
            "gate": to_jsonable_python([row.model_dump() for row in criteria_of(evaluation)]),
            "metrics": to_jsonable_python(evaluation.report.metrics.model_dump()),
            "thresholds": to_jsonable_python(evaluation.thresholds.model_dump()),
            "checks_passed": [
                "operating_days",
                "max_drawdown",
                "profit_factor",
                "min_trades",
                "min_losses",
                "daily_loss",
                "eligible_run",
                "phrase",
                "confirm_real_money",
                "reduced_capital",
                "key_verified_no_withdraw_ip_restricted",
                "no_live_run_active",
            ],
        }
        return await self._store(
            principal=principal,
            meta=meta,
            run_id=run_id,
            reason=body.reason,
            capital=body.initial_capital_quote,
            credential=credential,
            criteria=criteria,
            now=now,
        )

    async def _store(
        self,
        *,
        principal: Principal,
        meta: RequestMeta,
        run_id: str,
        reason: str,
        capital: Any,
        credential: ExchangeCredentialRow,
        criteria: dict[str, Any],
        now: datetime,
    ) -> LiveAuthorizationOut:
        policy = self._policy()
        authorization_id = uuid.uuid4().hex
        expires_at = now + policy.ttl
        key = self._mac_key()
        if key is None:
            raise Conflict("Live indisponivel: chave de assinatura das autorizacoes nao configurada.")
        record = authorization_record(
            authorization_id=authorization_id,
            paper_run_id=run_id,
            criteria=criteria,
            actor=principal.username,
            reason=reason,
            confirmed_phrase=LIVE_PHRASE,
            initial_capital_quote=capital,
            credential_id=str(credential.id),
            key_permissions=credential.permissions,
            created_at=now,
            expires_at=expires_at,
        )
        row = LiveAuthorizationRow(
            id=authorization_id,
            paper_run_id=run_id,
            criteria=criteria,
            actor=principal.username,
            reason=reason,
            confirmed_phrase=LIVE_PHRASE,
            initial_capital_quote=capital,
            credential_id=str(credential.id),
            key_permissions=credential.permissions,
            record_hash=authorization_mac(key, record),
            created_at=now,
            expires_at=expires_at,
        )
        async with self._c.sessions.begin() as db:
            await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _LOCK_KEY})
            if await self._active_authorization(now, db) is not None or await self._live_run_active(db):
                raise Conflict("Ja existe autorizacao ativa ou run Live em andamento.")
            db.add(row)
            await db.flush()
            await self._audit.record(
                actor=principal.username,
                action=AuditAction.LIVE_AUTHORIZATION_GRANTED,
                target=f"live:authorization:{authorization_id}",
                result=AuditResult.SUCCESS,
                meta=meta,
                details={
                    "paper_run_id": run_id,
                    "initial_capital_quote": str(capital),
                    "expires_at": expires_at.isoformat(),
                    "record_hash": row.record_hash,
                },
                session=db,
            )
        return _out(row, None, now)

    # ---- revogacao --------------------------------------------------------------------------------

    async def revoke(self, *, principal: Principal, reason: str, meta: RequestMeta) -> LiveAuthorizationOut:
        now = self._c.clock()
        async with self._c.sessions.begin() as db:
            await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _LOCK_KEY})
            row = await self._latest_unrevoked(db)
            if row is None:
                raise NotFound("Nao ha autorizacao do Live para revogar.")
            revocation = LiveAuthorizationRevocationRow(
                authorization_id=row.id, actor=principal.username, reason=reason, revoked_at=now
            )
            db.add(revocation)
            try:
                await db.flush()
            except IntegrityError:
                raise Conflict("A autorizacao ja foi revogada.") from None
            await self._audit.record(
                actor=principal.username,
                action=AuditAction.LIVE_AUTHORIZATION_REVOKED,
                target=f"live:authorization:{row.id}",
                result=AuditResult.SUCCESS,
                meta=meta,
                details={"reason": reason, "paper_run_id": row.paper_run_id},
                session=db,
            )
            return _out(row, revocation, now)

    # ---- leitura ----------------------------------------------------------------------------------

    async def state(self) -> LiveAuthorizationStateOut:
        c = self._c
        now = c.clock()
        async with c.sessions() as db:
            row = await db.scalar(
                select(LiveAuthorizationRow).order_by(LiveAuthorizationRow.created_at.desc()).limit(1)
            )
            revocation = await self._revocation_of(db, row)
            live_run = await latest_run(db, None, "live")
        out = _out(row, revocation, now, intact=self.row_intact(row)) if row is not None else None
        active = out is not None and out.active
        started = live_run.started_at if live_run and row and live_run.started_at >= row.created_at else None
        metrics = None
        if active and started is not None and live_run is not None:
            try:
                inputs = await collect_inputs(c.sessions, live_run.run_id, now=now, compare=False)
                from engine.application.paper_metrics import compute_paper_metrics

                metrics = compute_paper_metrics(
                    initial_equity=inputs.capital,
                    trades=inputs.trades,
                    curve=inputs.curve,
                    events=inputs.events,
                    now=now,
                )
            except UnknownRunError:
                log.info("live_state_without_journal", run_id=live_run.run_id)
        stage = live_stage(
            authorized=active,
            live_started_at=started,
            live_metrics=metrics,
            daily_loss_limit=(await _daily_loss_limit(c, live_run)),
            now=now,
        )
        return LiveAuthorizationStateOut(
            authorization=out,
            stage=stage.stage.value,
            days_in_live=stage.days_in_live,
            limit_increase_allowed=stage.limit_increase_allowed,
            next_step=stage.next_step,
            reduced_capital_days=REDUCED_CAPITAL_DAYS,
            limit_increase_days=LIMIT_INCREASE_DAYS,
            live_available=False,
        )

    # ---- apoio ------------------------------------------------------------------------------------

    def _mac_key(self) -> bytes | None:
        s = self._c.settings
        secret = s.live_authorization_hmac_key or s.command_hmac_key
        return secret.get_secret_value().encode() if secret is not None else None

    def row_intact(self, row: LiveAuthorizationRow) -> bool:
        """Reverifica o HMAC do registro gravado; adulteracao (ou chave trocada) invalida a autorizacao."""
        key = self._mac_key()
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
        intact = key is not None and authorization_mac_valid(key, record, row.record_hash)
        if not intact:
            log.error("live_authorization_mac_invalid", authorization_id=row.id)
        return intact

    def _policy(self) -> ActivationPolicy:
        s = self._c.settings
        return ActivationPolicy(
            initial_capital_cap=s.live_initial_capital_cap_quote,
            require_ip_restriction=s.live_require_ip_restriction,
            block_on_key_warnings=s.live_block_on_key_warnings,
            key_max_age=timedelta(hours=s.live_key_max_age_hours),
            ttl=timedelta(hours=s.live_authorization_hours),
        )

    async def _is_paper_run(self, run_id: str | None) -> bool:
        if run_id is None:
            return False
        async with self._c.sessions() as db:
            run = await db.get(RunRow, run_id)
        return run is not None and run.mode == "paper"

    async def _credential(
        self, credential_id: uuid.UUID | None, principal: Principal, meta: RequestMeta
    ) -> ExchangeCredentialRow | None:
        """Chave escolhida (ou a mais recente), RE-verificada agora na corretora."""
        async with self._c.sessions() as db:
            stmt = select(ExchangeCredentialRow).where(ExchangeCredentialRow.exchange == "binance")
            if credential_id is not None:
                stmt = stmt.where(ExchangeCredentialRow.id == credential_id)
            row = await db.scalar(stmt.order_by(ExchangeCredentialRow.created_at.desc()).limit(1))
        if row is None:
            return None
        try:
            return await CredentialService(self._c, self._audit).verify_stored(
                credential_id=row.id, actor=principal.username, meta=meta
            )
        except ApiError:
            row.last_verified_at = None
            return row

    async def _live_run_active(self, db: AsyncSession | None = None) -> bool:
        stmt = select(RunRow.run_id).where(RunRow.mode == "live", RunRow.ended_at.is_(None)).limit(1)
        if db is not None:
            return await db.scalar(stmt) is not None
        async with self._c.sessions() as own:
            return await own.scalar(stmt) is not None

    async def _latest_unrevoked(self, db: AsyncSession) -> LiveAuthorizationRow | None:
        revoked = select(LiveAuthorizationRevocationRow.authorization_id)
        stmt = (
            select(LiveAuthorizationRow)
            .where(LiveAuthorizationRow.id.not_in(revoked))
            .order_by(LiveAuthorizationRow.created_at.desc())
            .limit(1)
        )
        return await db.scalar(stmt)

    async def _active_authorization(
        self, now: datetime, db: AsyncSession | None = None
    ) -> LiveAuthorizationRow | None:
        async def find(session: AsyncSession) -> LiveAuthorizationRow | None:
            row = await self._latest_unrevoked(session)
            if row is None or row.expires_at <= now or not self.row_intact(row):
                return None
            return row

        if db is not None:
            return await find(db)
        async with self._c.sessions() as own:
            return await find(own)

    @staticmethod
    async def _revocation_of(
        db: AsyncSession, row: LiveAuthorizationRow | None
    ) -> LiveAuthorizationRevocationRow | None:
        if row is None:
            return None
        return await db.scalar(
            select(LiveAuthorizationRevocationRow).where(
                LiveAuthorizationRevocationRow.authorization_id == row.id
            )
        )

    async def _refuse(
        self,
        principal: Principal,
        meta: RequestMeta,
        unmet: tuple[Unmet, ...],
        inputs: ActivationInputs,
        *,
        credential_id: uuid.UUID | None,
    ) -> None:
        await self._audit.record(
            actor=principal.username,
            action=AuditAction.LIVE_ACTIVATION_REFUSED,
            target="live:activation",
            result=AuditResult.DENIED,
            meta=meta,
            details={
                "unmet": [{"criterion": u.code, "message": u.message} for u in unmet],
                "run_id": inputs.run_id,
                "gate_approved": inputs.gate_approved,
                "eligible_for_live": inputs.eligible,
                "phrase_sent": inputs.phrase is not None,
                "operating_days": inputs.metrics.operating_days,
                "credential_id": str(credential_id) if credential_id else None,
            },
        )


def _refusal(unmet: tuple[Unmet, ...]) -> Conflict:
    return Conflict(
        REFUSED_DETAIL,
        field_errors=[FieldError(loc=["criteria", u.code], message=u.message, code=u.code) for u in unmet],
    )


def _key_facts(row: ExchangeCredentialRow | None) -> KeyFacts | None:
    if row is None:
        return None
    permissions = KeyPermissions.model_validate(row.permissions)
    return KeyFacts(
        can_read=permissions.can_read,
        can_trade=permissions.can_trade,
        can_withdraw=permissions.can_withdraw,
        can_transfer=permissions.can_transfer,
        margin_enabled=permissions.margin_enabled,
        futures_enabled=permissions.futures_enabled,
        ip_restricted=permissions.ip_restricted,
        verified_at=row.last_verified_at,
    )


def _out(
    row: LiveAuthorizationRow,
    revocation: LiveAuthorizationRevocationRow | None,
    now: datetime,
    *,
    intact: bool = True,
) -> LiveAuthorizationOut:
    return LiveAuthorizationOut(
        id=row.id,
        paper_run_id=row.paper_run_id,
        actor=row.actor,
        reason=row.reason,
        initial_capital_quote=row.initial_capital_quote,
        created_at=row.created_at,
        expires_at=row.expires_at,
        revoked_at=revocation.revoked_at if revocation else None,
        revoked_by=revocation.actor if revocation else None,
        revocation_reason=revocation.reason if revocation else None,
        active=intact and revocation is None and row.expires_at > now,
        record_hash=row.record_hash,
        criteria=row.criteria,
    )


async def _daily_loss_limit(container: Container, live_run: RunRow | None) -> Any:
    from engine.domain.risk import RiskConfig

    return RiskConfig().max_daily_loss
