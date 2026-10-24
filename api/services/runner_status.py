"""Estado atual do runner: último evento no Redis (ou `status.json` quando há disco compartilhado)."""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import structlog

from api.container import Container
from engine.runner.paper_config import RunPaths, read_latest
from engine.runner.paper_control import read_status

log = structlog.get_logger()

Source = Literal["redis", "file", "none"]
# folga de relogio entre API e runner; acima disso o carimbo "do futuro" e suspeito, nao idade zero
MAX_CLOCK_AHEAD = timedelta(seconds=30)
# so estes eventos carregam estado/risco/versao do robo (o `equity` e so um ponto da curva)
CONTENT_KINDS = frozenset({"heartbeat", "state"})


@dataclass(frozen=True, slots=True)
class RunnerSnapshot:
    data: dict[str, Any] | None
    reachable: bool
    source: Source
    at: datetime | None
    age_seconds: float | None
    stale_after: float
    suspect: bool = False

    @property
    def alive(self) -> bool:
        return self.age_seconds is not None and self.age_seconds <= self.stale_after

    @property
    def connection(self) -> Literal["live", "stale", "offline", "unknown"]:
        if self.alive:
            return "live"
        if self.data is not None:
            return "stale"
        return "offline" if self.reachable else "unknown"

    @property
    def state(self) -> str | None:
        value = None if self.data is None else self.data.get("state")
        return value if isinstance(value, str) else None

    @property
    def run_id(self) -> str | None:
        value = None if self.data is None else self.data.get("run_id")
        return value if isinstance(value, str) else None

    @property
    def started_at(self) -> datetime | None:
        return None if self.data is None else parse_time(self.data.get("started_at"))

    @property
    def is_on(self) -> bool:
        """Robô ligado: com sinal de vida e fora de STOPPED/STOPPING."""
        return self.alive and self.state not in {"STOPPED", "STOPPING"}


def parse_time(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _stamp(data: dict[str, Any]) -> datetime | None:
    return parse_time(data.get("heartbeat_at")) or parse_time(data.get("at"))


class RunnerStatusService:
    def __init__(self, container: Container) -> None:
        self._c = container

    async def snapshot(self) -> RunnerSnapshot:
        """Vivo = o carimbo mais novo de QUALQUER evento valido; conteudo (estado, risco, versao) = SO do
        heartbeat/state mais novo: o `equity` nunca traz esses campos e nao pode mascara-los."""
        c = self._c
        stale = float(c.settings.runner_heartbeat_stale_seconds)
        reachable = False
        alive: datetime | None = None
        content: tuple[datetime, dict[str, Any]] | None = None
        fallback: tuple[datetime, dict[str, Any]] | None = None
        source: Source = "none"
        suspect = False
        if c.link is not None:
            probe = await c.link.probe()
            reachable = probe.reachable
            for kind, payload in probe.latest.items():
                stamp = _stamp(payload)
                if stamp is None:
                    continue
                if _from_the_future(stamp, c.clock()):
                    suspect = True
                    continue
                source = "redis"
                alive = stamp if alive is None or stamp > alive else alive
                if fallback is None or stamp > fallback[0]:
                    fallback = (stamp, payload)
                if kind in CONTENT_KINDS and (content is None or stamp > content[0]):
                    content = (stamp, payload)
        if (alive is None or not _recent(alive, c.clock(), stale)) and c.settings.runner_data_dir:
            from_file = await asyncio.to_thread(_read_file_status, c.settings.runner_data_dir)
            stamp = None if from_file is None else _stamp(from_file)
            if from_file is not None and stamp is not None and _from_the_future(stamp, c.clock()):
                suspect = True
            elif from_file is not None and stamp is not None and (alive is None or stamp > alive):
                alive, content, fallback, source = stamp, (stamp, from_file), (stamp, from_file), "file"
        if suspect:
            log.error("runner_heartbeat_from_the_future", ahead_limit_seconds=MAX_CLOCK_AHEAD.total_seconds())
        chosen = content or fallback
        if chosen is None or alive is None:
            return RunnerSnapshot(None, reachable, "none", None, None, stale, suspect)
        age = max(0.0, (c.clock() - alive).total_seconds())
        return RunnerSnapshot(chosen[1], reachable, source, alive, age, stale, suspect)


def _from_the_future(stamp: datetime, now: datetime) -> bool:
    return stamp - now > MAX_CLOCK_AHEAD


def _recent(stamp: datetime, now: datetime, stale: float) -> bool:
    return (now - stamp).total_seconds() <= stale


def _read_file_status(data_dir: Path) -> dict[str, Any] | None:
    try:
        run_id = read_latest(data_dir)
        return None if run_id is None else read_status(RunPaths(data_dir, run_id).status)
    except (OSError, ValueError):
        return None
