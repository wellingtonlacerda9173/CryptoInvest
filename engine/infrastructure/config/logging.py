import logging
from collections.abc import Mapping, MutableMapping
from typing import Any

import structlog

_SENSITIVE = ("password", "secret", "apikey", "token", "authorization", "signature")


def _is_sensitive(key: object) -> bool:
    normalized = str(key).lower().replace("_", "").replace("-", "")
    return any(marker in normalized for marker in _SENSITIVE)


def _scrub(value: Any) -> Any:
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
