import asyncio
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import structlog

from api.errors import TooManyRequests

log = structlog.get_logger()
LOG_EVERY_SECONDS = 10.0


class HashGate:
    """Limita quantos hashes Argon2 rodam ao mesmo tempo (cada um custa dezenas de MiB e uma thread).

    Passou do tempo de fila, a requisicao e recusada na hora (429): inundar o login nao esgota thread pool,
    memoria nem conexoes do banco, e as rotas autenticadas continuam respondendo.

    Limite de projeto: o gate e global, entao um atacante com muitos IPs (~25 req/s distintos mantem 4
    hashes de ~40 ms ocupados) faz o login legitimo ser recusado enquanto durar. Mitigue com rate limit no
    proxy; a saturacao e observavel em `rejected`/`peak` e no log `hash_gate_saturated` (1 a cada 10 s).
    """

    def __init__(
        self,
        max_concurrent: int,
        queue_timeout_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._max = max_concurrent
        self._timeout = queue_timeout_seconds
        self._clock = clock
        self._last_log = float("-inf")
        self.in_flight = 0
        self.peak = 0
        self.rejected = 0

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        try:
            await asyncio.wait_for(self._semaphore.acquire(), self._timeout)
        except TimeoutError:
            self._note_rejection()
            raise TooManyRequests("Servidor ocupado. Tente de novo em instantes.", retry_after=1) from None
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            yield
        finally:
            self.in_flight -= 1
            self._semaphore.release()

    def _note_rejection(self) -> None:
        self.rejected += 1
        now = self._clock()
        if now - self._last_log >= LOG_EVERY_SECONDS:
            self._last_log = now
            log.warning(
                "hash_gate_saturated", rejected_total=self.rejected, max_concurrent=self._max, peak=self.peak
            )
