from collections import deque
from datetime import datetime, timedelta

from engine.application.ai import AnalystOpinion


class CircuitBreaker:
    """Abre apos `threshold` falhas seguidas; depois de `cooldown` deixa passar uma tentativa de teste."""

    def __init__(self, threshold: int = 3, cooldown: timedelta = timedelta(minutes=5)) -> None:
        self._threshold = threshold
        self._cooldown = cooldown
        self._failures = 0
        self._opened_at: datetime | None = None

    def allows(self, now: datetime) -> bool:
        if self._opened_at is None:
            return True
        if now - self._opened_at >= self._cooldown:
            self._opened_at = now
            return True
        return False

    def success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def failure(self, now: datetime) -> None:
        self._failures += 1
        if self._failures >= self._threshold:
            self._opened_at = now

    @property
    def is_open(self) -> bool:
        return self._opened_at is not None

    @property
    def failures(self) -> int:
        return self._failures


class HourlyLimit:
    def __init__(self, max_calls: int) -> None:
        self._max = max_calls
        self._stamps: deque[datetime] = deque()

    def _trim(self, now: datetime) -> None:
        while self._stamps and now - self._stamps[0] >= timedelta(hours=1):
            self._stamps.popleft()

    def allows(self, now: datetime) -> bool:
        self._trim(now)
        return len(self._stamps) < self._max

    def hit(self, now: datetime) -> None:
        self._stamps.append(now)

    def used(self, now: datetime) -> int:
        self._trim(now)
        return len(self._stamps)


class ReplyCache:
    """Cache LRU limitado por hash do request: o mesmo pedido nao e pago duas vezes."""

    def __init__(self, size: int = 256) -> None:
        self._size = size
        self._items: dict[str, AnalystOpinion] = {}

    def get(self, key: str) -> AnalystOpinion | None:
        value = self._items.pop(key, None)
        if value is not None:
            self._items[key] = value
        return value

    def put(self, key: str, value: AnalystOpinion) -> None:
        self._items.pop(key, None)
        self._items[key] = value
        while len(self._items) > self._size:
            self._items.pop(next(iter(self._items)))
