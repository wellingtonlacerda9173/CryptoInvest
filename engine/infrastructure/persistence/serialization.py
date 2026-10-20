from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any, cast

from pydantic import BaseModel
from pydantic_core import to_jsonable_python
from sqlalchemy import CursorResult, Result

_SCALE = Decimal(1).scaleb(-18)


class PersistenceError(RuntimeError):
    """Estado do banco incompativel com a operacao pedida (conflito, posicao inexistente etc.)."""


class ConflictError(PersistenceError):
    """Registro ja existente com conteudo incompativel; o diario nunca e reescrito silenciosamente."""


def require_utc(value: datetime, name: str = "datetime") -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def quantize(value: Decimal) -> Decimal:
    """Mesma escala NUMERIC(38,18) do banco, para comparar o que entrou com o que ficou gravado."""
    return value.quantize(_SCALE, rounding=ROUND_HALF_EVEN)


def _check_aware(value: Any) -> None:
    match value:
        case datetime():
            require_utc(value, "datetime in JSON payload")
        case dict():
            for item in value.values():
                _check_aware(item)
        case list() | tuple():
            for item in value:
                _check_aware(item)


def to_json(value: Any) -> Any:
    """Decimal vira string (sem perda) e datetime vira ISO-8601; datetime sem fuso e recusado."""
    _check_aware(value)
    return to_jsonable_python(value)


def dump_model(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json")


def affected(result: Result[Any]) -> int:
    """Linhas afetadas por INSERT/UPDATE/DELETE (o stub generico de `Result` nao expoe `rowcount`)."""
    return cast("CursorResult[Any]", result).rowcount
