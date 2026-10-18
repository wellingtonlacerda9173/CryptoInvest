import random

_MAX_EXPONENT = 64


class ExponentialBackoff:
    """Backoff exponencial com teto e "equal jitter": atraso uniforme em [teto/2, teto]."""

    def __init__(self, base: float = 1.0, cap: float = 60.0, factor: float = 2.0) -> None:
        self._base = base
        self._cap = cap
        self._factor = factor
        self._attempt = 0

    def next_delay(self, minimum: float = 0.0) -> float:
        """`minimum` impoe um piso (ex.: Retry-After da corretora), acima do teto se preciso."""
        ceiling = min(self._cap, self._base * self._factor**self._attempt)
        self._attempt = min(self._attempt + 1, _MAX_EXPONENT)
        return max(minimum, random.uniform(ceiling / 2, ceiling))  # noqa: S311

    def reset(self) -> None:
        self._attempt = 0
