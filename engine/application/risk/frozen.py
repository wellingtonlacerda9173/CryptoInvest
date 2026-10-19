from collections.abc import Mapping
from typing import Any, NoReturn

from pydantic import AfterValidator


class FrozenDict[K, V](dict[K, V]):
    """dict sem mutacao in-place: o estado de risco so muda por transicoes que devolvem um novo estado."""

    def _blocked(self, *args: Any, **kwargs: Any) -> NoReturn:
        raise TypeError("FrozenDict is immutable")

    __setitem__ = __delitem__ = __ior__ = _blocked
    clear = pop = popitem = setdefault = update = _blocked

    def __reduce__(self) -> tuple[Any, ...]:
        return (FrozenDict, (dict(self),))

    def __copy__(self) -> "FrozenDict[K, V]":
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> "FrozenDict[K, V]":
        return FrozenDict(dict(self))


def _freeze(value: Mapping[Any, Any]) -> Mapping[Any, Any]:
    return value if isinstance(value, FrozenDict) else FrozenDict(value)


Frozen = AfterValidator(_freeze)
