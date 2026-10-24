"""Scores do seletor para o runner: ultimo snapshot, amostras do diario e publicacao (append-only)."""

import json
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from engine.application.selector import ScoreKey, StrategyScore, TradeSample
from engine.infrastructure.persistence.score_repository import Origin, ScoreRepository

DEFAULT_SAMPLES_DIR = Path("data/selector")


def samples_path(directory: Path, timeframe: str) -> Path:
    return directory / f"backtest_samples_{timeframe}.jsonl"


class ScoreStore(Protocol):
    async def latest_scores(
        self, timeframe: str, symbols: Sequence[str]
    ) -> dict[ScoreKey, StrategyScore]: ...

    async def journal_samples(self, symbols: Sequence[str]) -> list[TradeSample]: ...

    async def publish(
        self,
        scores: Sequence[StrategyScore],
        *,
        timeframe: str,
        origin: Origin,
        calculated_at: datetime,
        run_id: str | None = None,
    ) -> str: ...


class SqlScoreStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def latest_scores(self, timeframe: str, symbols: Sequence[str]) -> dict[ScoreKey, StrategyScore]:
        async with self._sessions() as session:
            return await ScoreRepository(session).latest(timeframe, symbols)

    async def journal_samples(self, symbols: Sequence[str]) -> list[TradeSample]:
        async with self._sessions() as session:
            return await ScoreRepository(session).journal_samples(symbols)

    async def publish(
        self,
        scores: Sequence[StrategyScore],
        *,
        timeframe: str,
        origin: Origin,
        calculated_at: datetime,
        run_id: str | None = None,
    ) -> str:
        async with self._sessions() as session, session.begin():
            return await ScoreRepository(session).publish(
                scores, timeframe=timeframe, origin=origin, calculated_at=calculated_at, run_id=run_id
            )


def write_samples(path: Path, samples: Sequence[TradeSample]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = (sample.model_dump_json() for sample in sorted(samples, key=lambda s: (s.closed_at, s.strategy)))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_samples(path: Path) -> list[TradeSample]:
    if not path.is_file():
        return []
    return [
        TradeSample.model_validate(json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
