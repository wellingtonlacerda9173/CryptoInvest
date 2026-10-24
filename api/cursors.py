"""Cursor de paginacao `<instante ISO com fuso>|<id>`: validado na borda (cursor ruim = 422, nunca 500)."""

import re
from datetime import datetime

from pydantic import BaseModel, TypeAdapter, ValidationError

from api.errors import FieldError, Unprocessable

_ID = re.compile(r"[0-9a-f]{32}")
_STAMP = TypeAdapter(datetime)


class TimeCursor(BaseModel):
    moment: datetime
    last_id: str

    def encode(self) -> str:
        return f"{self.moment.isoformat()}|{self.last_id}"


def encode_cursor(moment: datetime, last_id: str) -> str:
    return TimeCursor(moment=moment, last_id=last_id).encode()


def decode_cursor(raw: str) -> TimeCursor:
    stamp, sep, last_id = raw.partition("|")
    try:
        if not sep or not _ID.fullmatch(last_id):
            raise ValueError("id")
        moment = _STAMP.validate_python(stamp, strict=False)
        if moment.tzinfo is None:
            raise ValueError("fuso")
        return TimeCursor(moment=moment, last_id=last_id)
    except (ValueError, ValidationError, OverflowError):
        raise Unprocessable(
            "Cursor inválido: use o `next_cursor` devolvido pela listagem anterior.",
            field_errors=[FieldError(loc=["query", "cursor"], message="Cursor inválido.", code="invalid")],
        ) from None
