"""Primeiro acesso: o que já foi feito e a ação "Iniciar no Paper" (nunca vai direto ao Live)."""

from sqlalchemy import func, select

from api.audit import AuditAction, AuditResult, AuditService, RequestMeta
from api.container import Container
from api.errors import Conflict
from api.persistence.models import ExchangeCredentialRow, UserRow
from api.schemas_control import CommandOut, OnboardingOut, OnboardingStepOut, StartPaperOut
from api.services.commands import CommandService
from api.services.launcher import launch_runner
from api.services.runner_status import RunnerStatusService
from engine.application.trading_config import SECTIONS, default_document
from engine.domain.risk import RiskConfig
from engine.infrastructure.persistence.config_repository import ConfigRepository
from engine.infrastructure.persistence.models import RunRow
from engine.runner.paper_control import CommandName

_ = CommandOut


class OnboardingService:
    def __init__(self, container: Container, audit: AuditService | None = None) -> None:
        self._c = container
        self._audit = audit or AuditService(container.sessions)

    async def state(self) -> OnboardingOut:
        snapshot = await RunnerStatusService(self._c).snapshot()
        async with self._c.sessions() as db:
            admins = int(
                await db.scalar(
                    select(func.count())
                    .select_from(UserRow)
                    .where(UserRow.role == "admin", UserRow.disabled.is_(False))
                )
                or 0
            )
            with_2fa = int(
                await db.scalar(
                    select(func.count())
                    .select_from(UserRow)
                    .where(
                        UserRow.role == "admin", UserRow.disabled.is_(False), UserRow.totp_enabled.is_(True)
                    )
                )
                or 0
            )
            credentials = list((await db.scalars(select(ExchangeCredentialRow))).all())
            version = await ConfigRepository(db).latest()
            runs = int(
                await db.scalar(select(func.count()).select_from(RunRow).where(RunRow.mode == "paper")) or 0
            )
        usable = [
            c
            for c in credentials
            if (c.permissions or {}).get("can_read") and not (c.permissions or {}).get("can_withdraw")
        ]
        steps = [
            OnboardingStepOut(
                key="admin",
                title="Administrador criado",
                done=admins > 0,
                required=True,
                detail="Já existe um administrador."
                if admins
                else "Crie o primeiro administrador no servidor.",
            ),
            OnboardingStepOut(
                key="two_factor",
                title="Verificação em duas etapas (2FA) ativa",
                done=with_2fa > 0,
                required=True,
                detail="O 2FA está ativo." if with_2fa else "Configure o app autenticador no primeiro login.",
            ),
            OnboardingStepOut(
                key="binance_key",
                title="Chave da Binance salva e conferida",
                done=bool(usable),
                required=False,
                detail=(
                    f"Chave terminada em {usable[0].api_key_last4} conectada."
                    if usable
                    else "Opcional no Paper. Use chaves só de leitura e negociação spot, sem saque."
                ),
            ),
            OnboardingStepOut(
                key="risk",
                title="Risco configurado",
                done=version is not None,
                required=True,
                detail=(
                    "Os limites de risco estão salvos."
                    if version
                    else "Escolha quanto aceita perder por operação e por dia (ou use os padrões seguros)."
                ),
            ),
            OnboardingStepOut(
                key="paper",
                title="Paper iniciado",
                done=runs > 0 or snapshot.is_on,
                required=True,
                detail="O robô já rodou no Paper."
                if runs or snapshot.is_on
                else "Inicie no Paper: dinheiro de mentira.",
            ),
        ]
        pending = next((s.key for s in steps if s.required and not s.done), None)
        return OnboardingOut(
            steps=steps,
            completed=pending is None,
            next_step=pending,
            can_start_paper=admins > 0 and with_2fa > 0,
            paper_running=snapshot.is_on,
            message=(
                "Tudo pronto: seu robô está no Paper (dinheiro de mentira)."
                if pending is None
                else "Falta pouco para iniciar no Paper. Ficar parado em USDT é normal: "
                "o robô só entra quando o risco compensa."
            ),
        )

    async def start_paper(self, *, actor: str, meta: RequestMeta) -> StartPaperOut:
        version_id = await self._ensure_config(actor, meta)
        snapshot = await RunnerStatusService(self._c).snapshot()
        data = snapshot.data or {}
        result: StartPaperOut
        if snapshot.alive and snapshot.state not in {"STOPPED", "STOPPING"}:
            if data.get("halted"):
                raise Conflict("O robô está desligado por drawdown. Use Reativar em Controle.")
            if data.get("paused"):
                sent = await CommandService(self._c, self._audit).submit(
                    name=CommandName.RESUME,
                    operator=actor,
                    reason="Iniciar no Paper pelo painel",
                    actor=actor,
                    meta=meta,
                )
                result = StartPaperOut(
                    state="resumed",
                    message="Retomando o robô no Paper.",
                    config_version=version_id,
                    command_id=sent.id,
                )
            else:
                result = StartPaperOut(
                    state="already_running",
                    message="O robô já está rodando no Paper.",
                    config_version=version_id,
                )
        else:
            state, message = await launch_runner(self._c)
            result = StartPaperOut(state=state, message=message, config_version=version_id)
        await self._audit.record(
            actor=actor,
            action=AuditAction.ONBOARDING_PAPER_STARTED,
            target="paper:start",
            result=AuditResult.SUCCESS,
            meta=meta,
            details={"state": result.state, "config_version": version_id},
        )
        return result

    async def _ensure_config(self, actor: str, meta: RequestMeta) -> int:
        """Sem nenhuma versão gravada, o Paper começa com os padrões seguros (0,5% por operação)."""
        async with self._c.sessions.begin() as db:
            repo = ConfigRepository(db)
            latest = await repo.latest()
            if latest is not None:
                return latest.id
            saved = await repo.append(
                default_document(),
                actor=actor,
                reason="Padrões recomendados aplicados ao iniciar no Paper",
                activation="next_start",
                risk_increase=False,
                sections=list(SECTIONS),
                diff=[],
                expected_parent=None,
            )
            await self._audit.record(
                actor=actor,
                action=AuditAction.CONFIG_UPDATED,
                target=f"config:{saved.id}",
                result=AuditResult.SUCCESS,
                meta=meta,
                details={
                    "section": "all",
                    "reason": "padrões recomendados",
                    "config_hash": saved.config_hash,
                    "max_risk_per_trade": str(RiskConfig().risk_per_trade),
                },
                session=db,
            )
            return saved.id
