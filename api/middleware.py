import time

import structlog
from fastapi import HTTPException
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from api.context import set_request_id
from api.errors import PayloadTooLarge, problem_response, unexpected_error_response

log = structlog.get_logger()

CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"


def security_headers(hsts_max_age: int) -> list[tuple[str, str]]:
    headers = [
        ("Content-Security-Policy", CSP),
        ("X-Content-Type-Options", "nosniff"),
        ("X-Frame-Options", "DENY"),
        ("Referrer-Policy", "no-referrer"),
        ("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()"),
        ("Cross-Origin-Resource-Policy", "same-site"),
        ("Cache-Control", "no-store"),
    ]
    if hsts_max_age > 0:
        headers.append(("Strict-Transport-Security", f"max-age={hsts_max_age}; includeSubDomains"))
    return headers


class RequestContextMiddleware:
    """Mais externo: request id, cabecalhos de seguranca, log de acesso e rede de seguranca para excecoes."""

    def __init__(self, app: ASGIApp, *, hsts_max_age: int = 0) -> None:
        self.app = app
        self._security = security_headers(hsts_max_age)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = Headers(scope=scope).get("x-request-id")
        request_id = set_request_id(incoming)
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        started = time.perf_counter()
        status_holder = {"status": 500, "started": False}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                status_holder["started"] = True
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = request_id
                for name, value in self._security:
                    headers.setdefault(name, value)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            log.exception("unhandled_error", method=scope["method"], path=scope["path"])
            if not status_holder["started"]:
                response = unexpected_error_response(scope["path"])
                await response(scope, receive, send_wrapper)
        finally:
            client = scope.get("client")
            log.info(
                "http_request",
                method=scope["method"],
                path=scope["path"],
                status=status_holder["status"],
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
                client=client[0] if client else None,
            )
            structlog.contextvars.clear_contextvars()


class BodyLimitMiddleware:
    """Recusa corpo acima do limite: pelo Content-Length e, em streaming, contando os bytes recebidos."""

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self._max = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = Headers(scope=scope).get("content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > self._max):
            response = problem_response(
                413,
                slug="payload-too-large",
                title=PayloadTooLarge.title,
                detail=f"O corpo da requisicao excede {self._max} bytes.",
                instance=scope["path"],
            )
            await response(scope, receive, send)
            return
        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self._max:
                    raise HTTPException(
                        status_code=413, detail=f"O corpo da requisicao excede {self._max} bytes."
                    )
            return message

        await self.app(scope, limited_receive, send)
