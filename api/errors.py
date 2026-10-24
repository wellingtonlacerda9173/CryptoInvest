from collections.abc import Mapping
from http import HTTPStatus
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from api.context import current_request_id

log = structlog.get_logger()

PROBLEM_MEDIA_TYPE = "application/problem+json"
PROBLEM_BASE = "https://cryptoinvest.local/problems/"


class FieldError(BaseModel):
    loc: list[str | int]
    message: str
    code: str


class Problem(BaseModel):
    """Corpo de erro RFC 7807 (`application/problem+json`)."""

    type: str = Field(examples=[PROBLEM_BASE + "unauthorized"])
    title: str = Field(examples=["Nao autenticado"])
    status: int = Field(examples=[401])
    detail: str | None = Field(default=None, examples=["Sessao ausente, expirada ou revogada."])
    instance: str | None = Field(default=None, examples=["/auth/me"])
    request_id: str | None = Field(default=None, examples=["3f2b9c1d4e5a4b7c8d9e0f1a2b3c4d5e"])
    retry_after_seconds: int | None = None
    errors: list[FieldError] | None = None


class ApiError(Exception):
    status = 500
    slug = "internal"
    title = "Erro interno"

    def __init__(
        self,
        detail: str | None = None,
        *,
        headers: Mapping[str, str] | None = None,
        retry_after: int | None = None,
        field_errors: list[FieldError] | None = None,
    ) -> None:
        super().__init__(detail or self.title)
        self.detail = detail
        self.field_errors = field_errors
        self.headers = dict(headers or {})
        self.retry_after = retry_after
        if retry_after is not None:
            self.headers["Retry-After"] = str(retry_after)


class Unauthorized(ApiError):
    status, slug, title = 401, "unauthorized", "Nao autenticado"


class Forbidden(ApiError):
    status, slug, title = 403, "forbidden", "Acesso negado"


class NotFound(ApiError):
    status, slug, title = 404, "not-found", "Nao encontrado"


class Conflict(ApiError):
    status, slug, title = 409, "conflict", "Conflito"


class BadRequest(ApiError):
    status, slug, title = 400, "bad-request", "Requisicao invalida"


class PayloadTooLarge(ApiError):
    status, slug, title = 413, "payload-too-large", "Corpo da requisicao grande demais"


class TooManyRequests(ApiError):
    status, slug, title = 429, "too-many-requests", "Muitas tentativas"


class UpstreamUnavailable(ApiError):
    status, slug, title = 502, "upstream-unavailable", "Servico externo indisponivel"


class Unprocessable(ApiError):
    status, slug, title = 422, "validation-error", "Dados invalidos"


class FreshAuthRequired(Forbidden):
    slug, title = "fresh-auth-required", "Confirme sua identidade"


def problem_response(
    status: int,
    *,
    slug: str,
    title: str,
    detail: str | None = None,
    instance: str | None = None,
    headers: Mapping[str, str] | None = None,
    retry_after: int | None = None,
    errors: list[FieldError] | None = None,
) -> JSONResponse:
    body = Problem(
        type=PROBLEM_BASE + slug,
        title=title,
        status=status,
        detail=detail,
        instance=instance,
        request_id=current_request_id(),
        retry_after_seconds=retry_after,
        errors=errors,
    )
    return JSONResponse(
        body.model_dump(mode="json", exclude_none=True),
        status_code=status,
        media_type=PROBLEM_MEDIA_TYPE,
        headers=dict(headers or {}),
    )


def _on_api_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ApiError)  # noqa: S101
    return problem_response(
        exc.status,
        slug=exc.slug,
        title=exc.title,
        detail=exc.detail,
        instance=request.url.path,
        headers=exc.headers,
        retry_after=exc.retry_after,
        errors=exc.field_errors,
    )


def _on_http_exception(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)  # noqa: S101
    phrase = HTTPStatus(exc.status_code).phrase if exc.status_code in HTTPStatus else "Erro"
    return problem_response(
        exc.status_code,
        slug=phrase.lower().replace(" ", "-"),
        title=phrase,
        detail=exc.detail if isinstance(exc.detail, str) and exc.status_code != 404 else None,
        instance=request.url.path,
        headers=exc.headers,
    )


def _on_validation_error(request: Request, exc: Exception) -> JSONResponse:
    """Devolve so local, mensagem e codigo: NUNCA o valor recebido (`input`), que pode ser segredo."""
    assert isinstance(exc, RequestValidationError)  # noqa: S101
    errors = [
        FieldError(
            loc=[part for part in error["loc"] if part != "body"],
            message=str(error["msg"]),
            code=str(error["type"]),
        )
        for error in exc.errors()
    ]
    return problem_response(
        422,
        slug="validation-error",
        title="Dados invalidos",
        detail="Corrija os campos indicados e tente de novo.",
        instance=request.url.path,
        errors=errors,
    )


def unexpected_error_response(path: str) -> JSONResponse:
    return problem_response(
        500,
        slug="internal",
        title="Erro interno",
        detail="Algo deu errado. Informe o codigo da requisicao ao suporte.",
        instance=path,
    )


def install_error_handlers(app: FastAPI) -> None:
    handlers: dict[Any, Any] = {
        ApiError: _on_api_error,
        StarletteHTTPException: _on_http_exception,
        RequestValidationError: _on_validation_error,
    }
    for exc_type, handler in handlers.items():
        app.add_exception_handler(exc_type, handler)


PROBLEM_RESPONSES: dict[int | str, dict[str, Any]] = {
    status: {
        "model": Problem,
        "description": text,
        "content": {PROBLEM_MEDIA_TYPE: {"schema": {"$ref": "#/components/schemas/Problem"}}},
    }
    for status, text in {
        400: "Requisicao invalida",
        401: "Nao autenticado",
        403: "Sem permissao",
        404: "Nao encontrado",
        409: "Conflito",
        422: "Dados invalidos",
        429: "Muitas tentativas",
    }.items()
}
