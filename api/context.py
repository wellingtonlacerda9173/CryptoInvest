import re
import uuid
from contextvars import ContextVar

_REQUEST_ID: ContextVar[str | None] = ContextVar("request_id", default=None)
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9-]{8,64}$")


def current_request_id() -> str | None:
    return _REQUEST_ID.get()


def set_request_id(value: str | None) -> str:
    request_id = value if value and _REQUEST_ID_PATTERN.match(value) else uuid.uuid4().hex
    _REQUEST_ID.set(request_id)
    return request_id
