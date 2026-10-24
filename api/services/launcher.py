"""Ligar o robô pelo painel: só quando há um comando de início configurado (`API_RUNNER_START_COMMAND`).

O runner é um serviço separado. A API apenas dispara o processo (sem nenhuma lógica de trading); sem comando
configurado, o painel explica que o serviço precisa ser iniciado no servidor. O próprio runner impede duas
instâncias (trava de instância), então um disparo repetido é inofensivo.
"""

import asyncio
import os
import subprocess
import sys
import time
from typing import Literal, Protocol

import structlog

from api.container import Container
from api.services.runner_status import RunnerStatusService

log = structlog.get_logger()

LAUNCH_GUARD_SECONDS = 45.0
_LAST_LAUNCH_KEY = "launcher:last"


class RunnerLauncher(Protocol):
    async def launch(self) -> None: ...


class SubprocessLauncher:
    def __init__(self, command: list[str]) -> None:
        self._command = list(command)

    async def launch(self) -> None:
        await asyncio.to_thread(self._spawn)

    def _spawn(self) -> None:
        flags = 0
        if sys.platform == "win32":
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(  # noqa: S603 - comando vem da configuracao do operador, nunca da requisicao
            self._command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
            start_new_session=os.name == "posix",
            close_fds=True,
        )


async def launch_runner(container: Container) -> tuple[Literal["launched", "waiting_runner"], str]:
    """Dispara o runner se possível; nunca duas vezes em poucos segundos."""
    if container.launcher is None:
        return (
            "waiting_runner",
            "O serviço do robô está desligado no servidor. Inicie-o (python -m engine.runner.paper start) "
            "ou configure API_RUNNER_START_COMMAND para o painel poder ligá-lo.",
        )
    last = container.gate_cache.get(_LAST_LAUNCH_KEY)
    if last is not None and time.monotonic() - last[0] < LAUNCH_GUARD_SECONDS:
        return "launched", "O robô já está iniciando. Acompanhe em Controle."
    snapshot = await RunnerStatusService(container).snapshot()
    if snapshot.is_on:
        return "launched", "O robô já está ligado."
    container.gate_cache[_LAST_LAUNCH_KEY] = (time.monotonic(), True)
    await container.launcher.launch()
    log.info("runner_launch_requested")
    return "launched", "Iniciando o robô. Em instantes o estado muda para Operando."
