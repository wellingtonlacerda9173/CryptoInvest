import logging
import re
from collections.abc import Mapping, MutableMapping
from typing import Any

import structlog

_SENSITIVE = ("password", "secret", "apikey", "token", "authorization", "signature")


def _is_sensitive(key: object) -> bool:
    normalized = str(key).lower().replace("_", "").replace("-", "")
    return any(marker in normalized for marker in _SENSITIVE)


_MAX_SCRUB_CHARS = 4096
_URL_CREDENTIALS = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://[^:/\s@]+):[^/\s]+@")
_ANTHROPIC_KEY = re.compile(r"sk-ant-[A-Za-z0-9_-]{6,}")
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_AUTH_SCHEME = re.compile(
    r"(?i)(?P<key>\bauthorization[\"']?\s*[=:]\s*[\"']?)(?:basic|digest|negotiate|bearer)\s+[^\s,;\"')}\]]+"
)
_ASSIGNED_SECRET = re.compile(
    r"(?i)(?P<key>\b[\w-]{0,40}(?:password|passwd|secret|api[_-]?key|token|authorization|signature)[\w-]{0,40})"
    r"(?P<sep>[\"']?\s{0,8}[=:]\s{0,8})(?P<value>\"[^\"]*\"|'[^']*'|[^\s,;)}\]]+)"
)


def scrub_text(text: str) -> str:
    """Mascara segredos embutidos em texto livre (URL com senha, `token=...`, Bearer): erros e motivos."""
    if len(text) > _MAX_SCRUB_CHARS:
        text = text[:_MAX_SCRUB_CHARS] + "...[truncado]"
    text = _URL_CREDENTIALS.sub(r"\g<scheme>:***@", text)
    text = _AUTH_SCHEME.sub(lambda m: f"{m['key']}***", text)
    text = _ANTHROPIC_KEY.sub("sk-ant-***", text)
    text = _BEARER.sub("Bearer ***", text)
    return _ASSIGNED_SECRET.sub(lambda m: f"{m['key']}{m['sep']}***", text)


def _scrub(value: Any) -> Any:
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, Mapping):
        return {k: "***" if _is_sensitive(k) else _scrub(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_scrub(item) for item in value]
    return value


def _redact(_: Any, __: str, event_dict: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    return {k: "***" if _is_sensitive(k) else _scrub(v) for k, v in event_dict.items()}


def configure_logging(level: str = "INFO", *, json: bool = False) -> None:
    numeric = logging.getLevelNamesMapping().get(level.strip().upper())
    if numeric is None:
        raise ValueError(f"invalid log level: {level!r}")
    renderer = structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer()
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _redact,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric),
        cache_logger_on_first_use=True,
    )
