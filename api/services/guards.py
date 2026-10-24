"""Travas de seguranca que dependem do estado do robo."""

from api.audit import AuditAction, AuditResult, AuditService, RequestMeta
from api.container import Container
from api.errors import Conflict
from api.services.runner_status import RunnerStatusService

ROBOT_ON = "O robô está ligado. Pare o robô (Controle, Desligar) antes de remover a chave da Binance."
ROBOT_UNKNOWN = (
    "Não foi possível confirmar que o robô está parado (sem conexão com o canal do robô). "
    "Tente de novo quando a conexão voltar."
)


async def ensure_robot_stopped(
    container: Container, audit: AuditService, *, actor: str, target: str, meta: RequestMeta
) -> None:
    """Chave nao sai com o robô ligado; sem como saber o estado, tambem nao (na duvida, nao remove)."""
    snapshot = await RunnerStatusService(container).snapshot()
    if not (snapshot.is_on or snapshot.connection == "unknown"):
        return
    await audit.record(
        actor=actor,
        action=AuditAction.CREDENTIAL_DELETE_REFUSED,
        target=target,
        result=AuditResult.DENIED,
        meta=meta,
        details={"runner_state": snapshot.state, "connection": snapshot.connection},
    )
    raise Conflict(ROBOT_ON if snapshot.is_on else ROBOT_UNKNOWN)
