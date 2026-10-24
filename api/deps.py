import hmac
import ipaddress
import time
from datetime import timedelta
from typing import Annotated

import structlog
from fastapi import Depends, Request
from starlette.requests import HTTPConnection

from api.audit import AuditAction, AuditResult, AuditService, RequestMeta
from api.container import Container
from api.context import current_request_id
from api.errors import Forbidden, FreshAuthRequired, Unauthorized
from api.security.tokens import csrf_token
from api.services.sessions import Principal, SessionService

log = structlog.get_logger()
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
CSRF_HEADER = "X-CSRF-Token"


def get_container(request: HTTPConnection) -> Container:
    container: Container = request.app.state.container
    return container


ContainerDep = Annotated[Container, Depends(get_container)]


def _parse_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Endereco canonico: IPv4 mapeado em IPv6 (`::ffff:10.0.0.5`, comum em socket dual-stack) vira IPv4."""
    try:
        address = ipaddress.ip_address(host.strip())
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def _in_networks(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> bool:
    return any(address.version == network.version and address in network for network in networks)


def _forwarded_hops(request: HTTPConnection) -> list[str]:
    """Todos os saltos de TODAS as linhas `X-Forwarded-For`, na ordem em que chegaram.

    Proxies que ACRESCENTAM uma linha (HAProxy `option forwardfor`) em vez de concatenar na existente fazem
    `headers.get` devolver so a primeira, controlada pelo cliente."""
    joined = ",".join(request.headers.getlist("x-forwarded-for"))
    return [hop.strip() for hop in joined.split(",") if hop.strip()]


_FORWARDED_WARNING_EVERY_SECONDS = 300.0
_last_forwarded_warning = float("-inf")


def _warn_untrusted_forwarded(peer: str) -> None:
    """Header de proxy chegando com `trust_forwarded_for=false`: todos os clientes dividem o IP do proxy."""
    global _last_forwarded_warning
    now = time.monotonic()
    if now - _last_forwarded_warning >= _FORWARDED_WARNING_EVERY_SECONDS:
        _last_forwarded_warning = now
        log.warning(
            "forwarded_for_ignored_behind_proxy",
            peer=peer,
            hint="atras de proxy, defina API_TRUST_FORWARDED_FOR=true e API_TRUSTED_PROXIES; "
            "senao o throttle e a auditoria enxergam so o IP do proxy",
        )


def client_ip(request: HTTPConnection, container: Container) -> str | None:
    """IP do cliente. `X-Forwarded-For` so vale quando a conexao vem de um proxy confiavel.

    Conta os saltos a partir da DIREITA (os acrescentados pelos proxies confiaveis) e devolve o primeiro que
    nao e proxy confiavel; nunca o valor da esquerda, que o cliente controla."""
    raw_peer = request.client.host if request.client else None
    peer = _parse_ip(raw_peer) if raw_peer else None
    settings = container.settings
    shown_peer = str(peer) if peer is not None else (raw_peer[:45] if raw_peer else None)
    if not settings.trust_forwarded_for or raw_peer is None:
        if raw_peer is not None and request.headers.get("x-forwarded-for") is not None:
            _warn_untrusted_forwarded(raw_peer)
        return shown_peer
    trusted = settings.proxy_networks
    hops = _forwarded_hops(request)
    if not trusted:
        last = _parse_ip(hops[-1]) if hops else None
        return str(last) if last is not None else shown_peer
    if peer is None or not _in_networks(peer, trusted):
        return shown_peer
    for hop in reversed(hops):
        address = _parse_ip(hop)
        if address is None:
            return shown_peer
        if not _in_networks(address, trusted):
            return str(address)
    return shown_peer


def get_meta(request: HTTPConnection, container: ContainerDep) -> RequestMeta:
    return RequestMeta(
        ip=client_ip(request, container),
        user_agent=request.headers.get("user-agent", "")[:200] or None,
        request_id=current_request_id(),
    )


MetaDep = Annotated[RequestMeta, Depends(get_meta)]


def get_audit(container: ContainerDep) -> AuditService:
    return AuditService(container.sessions)


AuditDep = Annotated[AuditService, Depends(get_audit)]


async def audit_denial(
    request: HTTPConnection, container: Container, *, reason: str, actor: str, target: str
) -> None:
    """Registra a negacao (amostrada: um evento por janela por motivo/ator/IP, sem crescimento ilimitado)."""
    ip = client_ip(request, container)
    if not container.audit_sampler.should_emit(f"deny:{reason}:{actor}:{ip}"):
        return
    try:
        await AuditService(container.sessions).record(
            actor=actor,
            action=AuditAction.ACCESS_DENIED,
            target=target,
            result=AuditResult.DENIED,
            meta=RequestMeta(
                ip=ip,
                user_agent=request.headers.get("user-agent", "")[:200] or None,
                request_id=current_request_id(),
            ),
            details={"reason": reason, "path": request.scope.get("path", "")[:100]},
        )
    except Exception:
        log.exception("audit_denial_failed", reason=reason)


async def enforce_origin(request: HTTPConnection, container: ContainerDep) -> None:
    """Defesa em profundidade: metodo mutavel com `Origin` fora da lista do painel e recusado.

    O WebSocket tem a propria checagem de `Origin` (obrigatoria, no handler de `/ws`)."""
    if request.scope["type"] != "http" or request.scope["method"] in SAFE_METHODS:
        return
    origin = request.headers.get("origin")
    if origin is not None and origin.rstrip("/") not in container.settings.panel_origins:
        await audit_denial(request, container, reason="origin", actor="anonymous", target="origin")
        raise Forbidden("Origem nao autorizada.")


def expected_csrf(container: Container, principal: Principal) -> str:
    return csrf_token(container.session_secret, principal.session_id)


def _same(a: str, b: str) -> bool:
    """Comparacao em tempo constante sobre bytes: texto nao ASCII nao pode virar excecao (500)."""
    return hmac.compare_digest(a.encode("utf-8", "replace"), b.encode("utf-8", "replace"))


async def _check_csrf(request: Request, container: Container, principal: Principal) -> None:
    if request.method in SAFE_METHODS:
        return
    header = request.headers.get(CSRF_HEADER, "")
    cookie = request.cookies.get(container.settings.csrf_cookie_name, "")
    expected = expected_csrf(container, principal)
    if not (header and _same(header, cookie) and _same(header, expected)):
        await audit_denial(
            request,
            container,
            reason="csrf",
            actor=principal.username,
            target=f"session:{principal.session_id}",
        )
        raise Forbidden("Token CSRF ausente ou invalido.")


async def _authenticate(request: Request, container: Container, *, verified: bool) -> Principal:
    token = request.cookies.get(container.settings.session_cookie_name)
    principal = await SessionService(container).authenticate(token, verified=verified)
    if principal is None:
        raise Unauthorized(
            "Sessao ausente, expirada ou revogada." if verified else "Login pendente ausente ou expirado."
        )
    await _check_csrf(request, container, principal)
    return principal


async def require_user(request: Request, container: ContainerDep) -> Principal:
    """Qualquer usuario autenticado (senha + 2FA concluidos)."""
    return await _authenticate(request, container, verified=True)


async def require_pending(request: Request, container: ContainerDep) -> Principal:
    """Senha conferida, 2FA ainda pendente: unico estado aceito pelas rotas do segundo passo."""
    return await _authenticate(request, container, verified=False)


UserDep = Annotated[Principal, Depends(require_user)]
PendingDep = Annotated[Principal, Depends(require_pending)]


async def require_admin(principal: UserDep, meta: MetaDep, audit: AuditDep) -> Principal:
    if principal.role != "admin":
        await audit.record(
            actor=principal.username,
            action=AuditAction.ACCESS_DENIED,
            target=f"session:{principal.session_id}",
            result=AuditResult.DENIED,
            meta=meta,
            details={"role": principal.role},
        )
        raise Forbidden("Esta acao exige perfil de administrador.")
    return principal


AdminDep = Annotated[Principal, Depends(require_admin)]


async def require_fresh_auth(
    principal: UserDep, container: ContainerDep, request: HTTPConnection
) -> Principal:
    window = timedelta(minutes=container.settings.fresh_auth_minutes)
    fresh = principal.fresh_auth_at
    if fresh is None or container.clock() - fresh > window:
        await audit_denial(
            request,
            container,
            reason="fresh_auth",
            actor=principal.username,
            target=f"session:{principal.session_id}",
        )
        raise FreshAuthRequired(
            "Confirme sua identidade com o codigo do app autenticador (POST /auth/reauth) e tente de novo."
        )
    return principal


FreshDep = Annotated[Principal, Depends(require_fresh_auth)]


async def require_fresh_admin(
    principal: AdminDep, container: ContainerDep, request: HTTPConnection
) -> Principal:
    return await require_fresh_auth(principal, container, request)


FreshAdminDep = Annotated[Principal, Depends(require_fresh_admin)]
