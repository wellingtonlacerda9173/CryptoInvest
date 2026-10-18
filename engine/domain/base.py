import copy
from collections.abc import Iterable, Mapping
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, ValidationError


class DomainModel(BaseModel):
    """Base dos modelos com invariantes: imutavel e sem caminhos que pulem a validacao.

    `model_copy(update=)` e `model_construct()` revalidam; instancias aninhadas tambem sao revalidadas.
    `model_copy` rejeita chaves desconhecidas e ambos preservam `model_fields_set`.
    """

    model_config = ConfigDict(frozen=True, revalidate_instances="always")

    def model_copy(self, *, update: Mapping[str, Any] | None = None, deep: bool = False) -> Self:
        update = update or {}
        if unknown := [key for key in update if key not in type(self).model_fields]:
            raise ValidationError.from_exception_data(
                type(self).__name__,
                [{"type": "extra_forbidden", "loc": (key,), "input": update[key]} for key in unknown],
            )
        data = {**self.__dict__, **update}
        copy_ = type(self).model_validate(copy.deepcopy(data) if deep else data)
        _set_fields_set(copy_, self.model_fields_set | update.keys())
        return copy_

    @classmethod
    def model_construct(  # type: ignore[override]
        cls, _fields_set: set[str] | None = None, **values: Any
    ) -> Self:
        instance = cls.model_validate(values)
        if _fields_set is not None:
            _set_fields_set(instance, _fields_set)
        return instance


def _set_fields_set(instance: BaseModel, names: Iterable[str]) -> None:
    object.__setattr__(instance, "__pydantic_fields_set__", set(names))
