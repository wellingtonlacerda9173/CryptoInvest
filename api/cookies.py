from fastapi import Response

from api.config import ApiSettings
from api.security.tokens import csrf_token
from api.services.auth import Started


def set_session_cookies(response: Response, settings: ApiSettings, started: Started, secret: str) -> str:
    """Cookie de sessao httpOnly + cookie CSRF legivel pelo painel (double-submit com HMAC da sessao)."""
    csrf = csrf_token(secret, started.session_id)
    common = {
        "max_age": started.max_age_seconds,
        "path": "/",
        "secure": settings.cookie_secure,
        "samesite": "strict",
    }
    response.set_cookie(settings.session_cookie_name, started.token, httponly=True, **common)  # type: ignore[arg-type]
    response.set_cookie(settings.csrf_cookie_name, csrf, httponly=False, **common)  # type: ignore[arg-type]
    return csrf


def clear_session_cookies(response: Response, settings: ApiSettings) -> None:
    for name, http_only in ((settings.session_cookie_name, True), (settings.csrf_cookie_name, False)):
        response.delete_cookie(
            name, path="/", secure=settings.cookie_secure, httponly=http_only, samesite="strict"
        )
