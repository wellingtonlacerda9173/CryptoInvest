"""Canal de comandos do operador: arquivos-sentinela atomicos lidos pelo runner (sem depender de rede).

SEGURANCA: este canal NAO autentica ninguem. A unica barreira e a permissao do diretorio de comandos
(0700 onde o sistema operacional permite) mais as travas abaixo: comandos expiram (TTL) e os emitidos antes
da partida do processo sao descartados (o `emergency-stop` simples, que apenas reduz risco, sempre vale; com
`--liquidate` vale por `LIQUIDATE_TTL` e depois exige reemissao). Quem
puder gravar no diretorio do run controla o robo. A API da Fase 4 substitui este canal por autenticacao/2FA e
autorizacao por papel; ate la, `reactivate` e `emergency-stop --liquidate` exigem operador e motivo
explicitos, registrados no `audit_log` junto do hash do comando.
"""

import hashlib
import json
import os
import time
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

import structlog
from pydantic import Field, ValidationError

from engine.domain.base import DomainModel
from engine.domain.types import UtcDatetime
from engine.infrastructure.config.logging import scrub_text

log = structlog.get_logger()

COMMAND_TTL = timedelta(minutes=10)
LIQUIDATE_TTL = timedelta(minutes=30)
_REPLACE_ATTEMPTS = 4
_REPLACE_PAUSE_SECONDS = 0.02


class CommandName(StrEnum):
    PAUSE = "pause"
    RESUME = "resume"
    STOP = "stop"
    EMERGENCY_STOP = "emergency-stop"
    REACTIVATE = "reactivate"


class Command(DomainModel):
    name: CommandName
    operator: str = Field(min_length=1, max_length=100, pattern=r"\S")
    reason: str = Field(min_length=1, pattern=r"\S")
    liquidate: bool = False
    issued_at: UtcDatetime

    @property
    def sensitive(self) -> bool:
        """Religar apos drawdown e liquidar a mercado: acoes que exigem identificacao explicita."""
        return self.name is CommandName.REACTIVATE or (
            self.name is CommandName.EMERGENCY_STOP and self.liquidate
        )

    def digest(self) -> str:
        """SHA-256 do conteudo canonico do comando (vai para o `audit_log`)."""
        raw = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()


def ensure_private_dir(directory: Path) -> None:
    """Cria o diretorio; onde ha permissoes POSIX restringe ao dono (no Windows vale a ACL do perfil)."""
    directory.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        directory.chmod(0o700)


class CommandQueue:
    """Fila em diretorio: o CLI escreve `<instante>-<nome>.json`; o runner consome em ordem e arquiva."""

    def __init__(self, directory: Path, *, ttl: timedelta = COMMAND_TTL) -> None:
        self._dir = directory
        self._ttl = ttl

    def send(self, command: Command) -> Path:
        ensure_private_dir(self._dir)
        stamp = command.issued_at.astimezone(UTC).strftime("%Y%m%dT%H%M%S%f")
        target = self._dir / f"{stamp}-{command.name.value}.json"
        temp = target.with_suffix(".tmp")
        temp.write_text(command.model_dump_json(), encoding="utf-8")
        if os.name == "posix":
            temp.chmod(0o600)
        os.replace(temp, target)
        return target

    def pending(
        self, now: datetime | None = None, *, not_before: datetime | None = None
    ) -> list[tuple[Path, Command]]:
        """Comandos validos em ordem. Com `now`, os mais velhos que o TTL sao arquivados como expirados; com
        `not_before`, os emitidos antes dele (partida do processo) tambem. `emergency-stop` simples nunca
        expira; com `--liquidate` expira em `LIQUIDATE_TTL`."""
        if not self._dir.is_dir():
            return []
        found: list[tuple[Path, Command]] = []
        for path in sorted(self._dir.glob("*.json")):
            try:
                command = Command.model_validate_json(path.read_text(encoding="utf-8"))
            except (ValidationError, ValueError, OSError) as error:
                log.error("paper_command_invalid", file=path.name, error=scrub_text(repr(error)))
                self._archive(path, "rejected")
                continue
            if self._expired(command, now, not_before):
                log.warning("paper_command_expired", file=path.name, issued_at=command.issued_at.isoformat())
                self._archive(path, "expired")
                continue
            found.append((path, command))
        return found

    def _expired(self, command: Command, now: datetime | None, not_before: datetime | None) -> bool:
        if command.name is CommandName.EMERGENCY_STOP:
            return command.liquidate and now is not None and now - command.issued_at > LIQUIDATE_TTL
        return self._stale(command, now, not_before)

    def _stale(self, command: Command, now: datetime | None, not_before: datetime | None) -> bool:
        if now is not None and now - command.issued_at > self._ttl:
            return True
        return not_before is not None and command.issued_at < not_before

    def done(self, path: Path) -> None:
        self._archive(path, "processed")

    def register_failure(self, path: Path) -> int:
        """Conta tentativas falhas no disco (sobrevive a reinicios do processo)."""
        counter = path.with_suffix(".attempts")
        try:
            previous = int(counter.read_text(encoding="utf-8").strip() or 0)
        except (OSError, ValueError):
            previous = 0
        counter.write_text(str(previous + 1), encoding="utf-8")
        return previous + 1

    def fail(self, path: Path) -> None:
        self._archive(path, "failed")

    def _archive(self, path: Path, folder: str) -> None:
        target = self._dir / folder
        ensure_private_dir(target)
        os.replace(path, target / path.name)
        path.with_suffix(".attempts").unlink(missing_ok=True)


def write_status(path: Path, payload: dict[str, Any]) -> bool:
    """Escrita atomica: o CLI `status` nunca le um arquivo pela metade.

    Um leitor com o arquivo aberto faz `os.replace` falhar no Windows (PermissionError): tenta de novo e, se
    nao der, so registra. O status e informativo; nunca pode derrubar o robo.
    """
    temp = path.with_suffix(".tmp")
    error: OSError | None = None
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp.write_text(json.dumps(payload, default=str, indent=2), encoding="utf-8")
            os.replace(temp, path)
            return True
        except OSError as caught:
            error = caught
            if attempt + 1 < _REPLACE_ATTEMPTS:
                time.sleep(_REPLACE_PAUSE_SECONDS)
    log.warning("paper_status_write_failed", path=str(path), error=scrub_text(repr(error)))
    return False


def read_status(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def now_utc() -> datetime:
    return datetime.now(UTC)
