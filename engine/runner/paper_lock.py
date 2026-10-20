"""Instancia unica do robo: lock de arquivo (mesma maquina) e advisory lock no Postgres (qualquer maquina).

Dois robos no mesmo run (ou no mesmo simbolo) gravariam ordens e estado um por cima do outro. Ambos os locks
sao liberados pelo sistema operacional/servidor se o processo morrer, entao uma queda nunca deixa o run preso.
"""

import contextlib
import os
import sys
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import IO

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

_PID_OFFSET = 16


class AlreadyRunningError(RuntimeError):
    """Ja existe um robo ativo para este run/simbolo."""


class InstanceLock:
    """Lock exclusivo do SO sobre `<run>/robot.lock`; o PID gravado serve apenas para a mensagem de erro."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle: IO[bytes] | None = None

    def acquire(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = self._path.open("a+b")
        try:
            self._lock(handle)
        except OSError:
            holder = self._holder(handle)
            handle.close()
            raise AlreadyRunningError(
                f"ja existe um robo em execucao para este run{f' (PID {holder})' if holder else ''}: "
                f"{self._path}. Pare-o (comando `stop`) antes de iniciar outro."
            ) from None
        handle.seek(_PID_OFFSET)
        handle.truncate()
        handle.write(str(os.getpid()).encode())
        handle.flush()
        self._handle = handle

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        with contextlib.suppress(OSError):
            self._unlock(handle)
        handle.close()

    @staticmethod
    def _lock(handle: IO[bytes]) -> None:
        handle.seek(0)
        if sys.platform == "win32":
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock(handle: IO[bytes]) -> None:
        handle.seek(0)
        if sys.platform == "win32":
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _holder(handle: IO[bytes]) -> str:
        try:
            handle.seek(_PID_OFFSET)
            return handle.read(16).decode(errors="ignore").strip()
        except OSError:
            return ""


@contextlib.asynccontextmanager
async def database_locks(engine: AsyncEngine, run_id: str, symbols: Sequence[str]) -> AsyncIterator[None]:
    """Advisory locks de sessao (um por run e um por simbolo) numa conexao dedicada durante todo o run."""
    keys = [f"cryptoinvest:paper:run:{run_id}", *(f"cryptoinvest:paper:symbol:{s}" for s in sorted(symbols))]
    async with engine.connect() as raw:
        connection = await raw.execution_options(isolation_level="AUTOCOMMIT")
        acquired: list[str] = []
        try:
            for key in keys:
                if not await _try_lock(connection, key):
                    raise AlreadyRunningError(
                        f"outro robo ja detem o lock '{key.removeprefix('cryptoinvest:paper:')}' no banco; "
                        "dois robos no mesmo run/simbolo nao sao permitidos"
                    )
                acquired.append(key)
            yield
        finally:
            # a conexao volta ao pool: nenhum lock de sessao pode vazar com ela
            with contextlib.suppress(Exception):
                for key in acquired:
                    await connection.execute(
                        text("SELECT pg_advisory_unlock(hashtextextended(:key, 0))"), {"key": key}
                    )


async def _try_lock(connection: AsyncConnection, key: str) -> bool:
    locked = await connection.scalar(
        text("SELECT pg_try_advisory_lock(hashtextextended(:key, 0))"), {"key": key}
    )
    return bool(locked)
