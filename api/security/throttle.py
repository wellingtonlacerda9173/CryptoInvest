import ipaddress
import time
from collections import defaultdict, deque
from collections.abc import Callable

import structlog

log = structlog.get_logger()

LOCKOUT_THRESHOLD = 5
LOCKOUT_BASE_SECONDS = 30
LOCKOUT_CAP_SECONDS = 3600
SATURATED_RETRY_SECONDS = 30
IPV6_PREFIX_BITS = 64


def throttle_ip(ip: str | None) -> str:
    """Chave de IP do throttle: IPv4 inteiro; IPv6 pelo prefixo /64 (um host controla o /64 inteiro e giraria
    enderecos para fugir do bloqueio). IPv4 mapeado em IPv6 vira o IPv4."""
    if not ip:
        return "unknown"
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return ip[:45]
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return f"{ipaddress.ip_network((address, IPV6_PREFIX_BITS), strict=False)}"
    return str(address)


def lockout_seconds(failures: int, threshold: int = LOCKOUT_THRESHOLD) -> int:
    """Bloqueio progressivo: na falha `threshold` bloqueia 30 s e dobra a cada falha seguinte ate 1 h."""
    if failures < threshold:
        return 0
    return int(min(LOCKOUT_BASE_SECONDS * 2 ** min(failures - threshold, 12), LOCKOUT_CAP_SECONDS))


class FailureThrottle:
    """Contador em memoria de falhas por chave, com bloqueio duro progressivo.

    Vale por processo (varios workers = estados independentes; ver README). A memoria e limitada: por chave
    (`threshold + 16` eventos) e no total (`max_keys`); ao lotar, saem primeiro as chaves sem bloqueio ativo,
    nunca um bloqueio vigente. Lotado so de bloqueios ativos, a politica e fail-closed: chave NOVA recebe
    `retry_after` generico (429) em vez de passar sem contagem, ate algum bloqueio expirar.
    """

    def __init__(
        self,
        *,
        threshold: int = LOCKOUT_THRESHOLD,
        window_seconds: float = 900.0,
        max_keys: int = 50_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._threshold = threshold
        self._window = window_seconds
        self._max_keys = max_keys
        self._clock = clock
        self._events_per_key = threshold + 16
        self._failures: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=self._events_per_key))
        self._locked_until: dict[str, float] = {}
        self._last_evict = float("-inf")
        self.saturated_refusals = 0

    def retry_after(self, key: str) -> int:
        remaining = self._locked_until.get(key, 0.0) - self._clock()
        if remaining > 0:
            return int(remaining) + 1
        if key not in self._failures and self._full():
            self.saturated_refusals += 1
            if self.saturated_refusals == 1 or self.saturated_refusals % 1000 == 0:
                log.warning("throttle_saturated", max_keys=self._max_keys, refusals=self.saturated_refusals)
            return SATURATED_RETRY_SECONDS
        return 0

    def _full(self) -> bool:
        """Lotado so de bloqueios ativos (a evicao a cada 1 s libera o que expirou)."""
        if len(self._failures) < self._max_keys:
            return False
        now = self._clock()
        if now - self._last_evict >= 1.0:
            self._last_evict = now
            self._evict(now)
        return len(self._failures) >= self._max_keys

    def clear_prefix(self, prefix: str) -> None:
        """Solta todas as chaves que comecam por `prefix` (desbloqueio administrativo; custo O(n), raro)."""
        for key in [k for k in (*self._failures, *self._locked_until) if k.startswith(prefix)]:
            self._drop(key)

    def register_failure(self, key: str) -> int:
        """Registra a falha (ou a reserva da tentativa, antes de verificar); devolve o bloqueio resultante."""
        now = self._clock()
        if key not in self._failures and len(self._failures) >= self._max_keys:
            self._evict(now)
            if len(self._failures) >= self._max_keys:
                return 0
        events = self._failures[key]
        events.append(now)
        while events and now - events[0] > self._window:
            events.popleft()
        delay = lockout_seconds(len(events), self._threshold)
        if delay:
            self._locked_until[key] = now + delay
        return delay

    def refund(self, key: str) -> None:
        """Devolve a reserva mais recente (tentativa que acabou valida) e solta o bloqueio que ela causou."""
        events = self._failures.get(key)
        if not events:
            return
        events.pop()
        if len(events) < self._threshold:
            self._locked_until.pop(key, None)
        if not events:
            self._failures.pop(key, None)

    def register_success(self, key: str) -> None:
        self._failures.pop(key, None)
        self._locked_until.pop(key, None)

    def _locked(self, key: str, now: float) -> bool:
        return self._locked_until.get(key, 0.0) > now

    def _evict(self, now: float) -> None:
        for key in [k for k, ev in self._failures.items() if (not ev or now - ev[-1] > self._window)]:
            if not self._locked(key, now):
                self._drop(key)
        for key in [k for k, until in self._locked_until.items() if until <= now and k not in self._failures]:
            self._locked_until.pop(key, None)
        if len(self._failures) >= self._max_keys:
            unlocked = [k for k in self._failures if not self._locked(k, now)]
            if unlocked:
                self._drop(min(unlocked, key=lambda k: self._failures[k][-1]))

    def _drop(self, key: str) -> None:
        self._failures.pop(key, None)
        self._locked_until.pop(key, None)


class UserPressure:
    """Pressao de falhas por usuario (de todos os IPs): NUNCA bloqueia, so atrasa e sinaliza.

    Bloqueio duro por usuario deixaria qualquer anonimo trancar o admin legitimo; o bloqueio duro vale
    apenas por par (usuario, IP). Aqui o atraso e progressivo e limitado a `delay_cap_seconds`.
    """

    def __init__(
        self,
        *,
        free_attempts: int = 3,
        base_seconds: float = 0.25,
        cap_seconds: float = 2.0,
        window_seconds: float = 900.0,
        max_keys: int = 50_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._free = free_attempts
        self._base = base_seconds
        self._cap = cap_seconds
        self._window = window_seconds
        self._max_keys = max_keys
        self._clock = clock
        self._events: dict[str, deque[float]] = {}

    def count(self, key: str) -> int:
        events = self._events.get(key)
        if not events:
            return 0
        now = self._clock()
        while events and now - events[0] > self._window:
            events.popleft()
        return len(events)

    def register_failure(self, key: str) -> int:
        now = self._clock()
        if key not in self._events and len(self._events) >= self._max_keys:
            self._events = {k: v for k, v in self._events.items() if v and now - v[-1] <= self._window}
            if len(self._events) >= self._max_keys:
                return 0
        events = self._events.setdefault(key, deque(maxlen=256))
        events.append(now)
        return self.count(key)

    def clear(self, key: str) -> None:
        self._events.pop(key, None)

    def delay_seconds(self, key: str) -> float:
        over = self.count(key) - self._free
        if over < 0:
            return 0.0
        return float(min(self._cap, self._base * 2**over))


class LockEpochs:
    """Ultimo `users.lock_epoch` visto por usuario (tag HMAC) neste processo.

    O desbloqueio administrativo incrementa a coluna no banco; este registro e como OUTRO processo (CLI, outro
    worker) descobre que o bloqueio em memoria deste processo ja foi liberado, sem consultar o banco a cada
    tentativa: so quando ha bloqueio e no maximo a cada `recheck_seconds` por usuario.
    """

    def __init__(
        self,
        *,
        recheck_seconds: float = 5.0,
        max_keys: int = 50_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._recheck = recheck_seconds
        self._max_keys = max_keys
        self._clock = clock
        self._seen: dict[str, int] = {}
        self._checked: dict[str, float] = {}

    def changed(self, tag: str, epoch: int) -> bool:
        """Registra a epoca vista; True se mudou desde a anterior (houve desbloqueio em outro processo)."""
        previous = self._seen.get(tag)
        if previous is None and len(self._seen) >= self._max_keys:
            self._seen.pop(next(iter(self._seen)))
        self._seen[tag] = epoch
        return previous is not None and previous != epoch

    def due(self, tag: str) -> bool:
        now = self._clock()
        last = self._checked.get(tag)
        if last is not None and now - last < self._recheck:
            return False
        if tag not in self._checked and len(self._checked) >= self._max_keys:
            self._checked.pop(next(iter(self._checked)))
        self._checked[tag] = now
        return True


class AuditSampler:
    """Um evento por chave por janela: evita uma linha de auditoria por tentativa bloqueada (429)."""

    def __init__(
        self,
        *,
        window_seconds: float = 60.0,
        max_keys: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._window = window_seconds
        self._max_keys = max_keys
        self._clock = clock
        self._last: dict[str, float] = {}

    def should_emit(self, key: str) -> bool:
        now = self._clock()
        last = self._last.get(key)
        if last is not None and now - last < self._window:
            return False
        if key not in self._last and len(self._last) >= self._max_keys:
            self._last = {k: t for k, t in self._last.items() if now - t < self._window}
            if len(self._last) >= self._max_keys:
                return False
        self._last[key] = now
        return True
