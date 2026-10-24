"""Backtests do painel: jobs em tabela, executados em SUBPROCESSO (fora do event loop da API).

Limites: no máximo `backtest_max_concurrent` jobs por vez (os demais esperam em `queued`), tempo máximo por
job (o processo é morto) e memória máxima (vigia dentro do worker). Cancelável a qualquer momento.
"""

import asyncio
import contextlib
import json
import shutil
import sys
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import structlog
from sqlalchemy import select

from api.audit import AuditAction, AuditResult, AuditService, RequestMeta
from api.container import Container
from api.cursors import decode_cursor, encode_cursor
from api.errors import Conflict, NotFound, Unprocessable
from api.schemas_backtest import (
    MIN_START,
    BacktestCreate,
    BacktestJobOut,
    BacktestPage,
    BacktestReportOut,
    JobStatus,
    ProgressOut,
)
from engine.infrastructure.config.logging import scrub_text
from engine.infrastructure.persistence.models import BacktestJobRow
from engine.runner.backtest import DEFAULT_GRID, parse_grid
from engine.runner.candle_source import floor_to_timeframe

log = structlog.get_logger()

ROOT = Path(__file__).resolve().parents[2]
DISCLAIMER = "Resultado de simulação no passado. Não garante o futuro."
MAX_GRID_COMBINATIONS = 24
ACTIVE = ("queued", "running")
YEAR_DAYS = 365
# o dono do subprocesso (processo da API) renova este sinal; outro processo so o considera morto sem ele
OWNER_BEAT_SECONDS = 10.0
OWNER_DEAD_AFTER_SECONDS = 45.0
SWEEP_INTERVAL_SECONDS = 15.0


def job_out(row: BacktestJobRow) -> BacktestJobOut:
    progress = row.progress or {}
    return BacktestJobOut(
        id=row.id,
        status=cast("JobStatus", row.status),
        created_by=row.created_by,
        created_at=row.created_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        cancel_requested=row.cancel_requested,
        params=row.params,
        progress=ProgressOut(
            stage=str(progress.get("stage", "")),
            percent=float(progress.get("percent", 0)),
            message=str(progress.get("message", "")),
            symbol=progress.get("symbol"),
            updated_at=_parse(progress.get("updated_at")),
        ),
        error=row.error,
        has_report=row.result is not None,
    )


def _parse(raw: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(raw)) if raw else None
    except ValueError:
        return None


def resolve_params(body: BacktestCreate, now: datetime, max_years: float) -> dict[str, Any]:
    """Período e premissas já resolvidos (o job grava exatamente o que vai rodar)."""
    if body.years is not None:
        if body.years > max_years:
            raise Unprocessable(f"O período máximo é de {max_years:g} anos por backtest.")
        end = floor_to_timeframe(now, body.timeframe)
        start = end - timedelta(days=round(body.years * YEAR_DAYS))
    else:
        assert body.start is not None  # noqa: S101
        assert body.end is not None  # noqa: S101
        start = floor_to_timeframe(body.start.astimezone(UTC), body.timeframe)
        end = floor_to_timeframe(body.end.astimezone(UTC), body.timeframe)
        if (end - start).days > max_years * YEAR_DAYS:
            raise Unprocessable(f"O período máximo é de {max_years:g} anos por backtest.")
        if end > now:
            raise Unprocessable("O período não pode terminar no futuro.")
    if start < MIN_START:
        raise Unprocessable(f"Não há dados antes de {MIN_START:%d/%m/%Y}: reduza o período.")
    days = (end - start).days
    if days < 2:
        raise Unprocessable("O período precisa ter ao menos 2 dias.")
    if body.walk_forward and days < body.train_days + body.test_days:
        raise Unprocessable(
            f"Período curto demais para o teste em janelas: precisa de pelo menos "
            f"{body.train_days + body.test_days} dias (treino {body.train_days} + teste {body.test_days}) "
            f"e você pediu {days}."
        )
    if body.walk_forward:
        try:
            combos = len(parse_grid(body.grid or DEFAULT_GRID).combinations())
        except (ValueError, ArithmeticError) as error:
            raise Unprocessable(f"Grade de parâmetros inválida: {error}") from None
        if combos > MAX_GRID_COMBINATIONS:
            raise Unprocessable(
                f"Combinações demais ({combos}): o limite é {MAX_GRID_COMBINATIONS}. "
                "Quanto mais combinações, maior o risco de enganar-se com sobreajuste."
            )
    data = body.model_dump(mode="json", exclude={"years", "start", "end"})
    return {**data, "start": start.isoformat(), "end": end.isoformat(), "days": days}


class BacktestRunner:
    """Fila + execução dos jobs. Um único processo da API é o dono dos subprocessos."""

    def __init__(self, container: Container) -> None:
        self._c = container
        self._slots = asyncio.Semaphore(container.settings.backtest_max_concurrent)
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._procs: dict[str, asyncio.subprocess.Process] = {}
        self._owner = uuid.uuid4().hex

    async def recover(self) -> None:
        """Início da API: jobs que estavam rodando morreram junto (o subprocesso perde o pipe do dono); os
        que esperavam voltam para a fila. Um job `running` cujo DONO (outro processo da API) ainda renova o
        sinal de vida NÃO é marcado como falho. O desenho suporta UM processo da API (single-worker): com
        vários, só o dono consome a saída do subprocesso."""
        now = self._c.clock()
        async with self._c.sessions.begin() as db:
            rows = (await db.scalars(select(BacktestJobRow).where(BacktestJobRow.status.in_(ACTIVE)))).all()
            queued = []
            for row in rows:
                if row.status == "running" and self._owned_elsewhere_and_alive(row, now):
                    log.warning("backtest_owned_by_another_api_process", job=row.id)
                elif row.status == "running":
                    row.status = "failed"
                    row.error = "Interrompido: a API reiniciou durante a execução. Rode de novo."
                    row.finished_at = now
                else:
                    queued.append(row.id)
        for job_id in queued:
            self.submit(job_id)

    async def sweep(self) -> int:
        """Manutencao periodica: `running` sem dono vivo (a API caiu e voltou em menos que `OWNER_DEAD_AFTER_
        SECONDS`, entao o `recover` do inicio ainda o via vivo) vira falho assim que o sinal de vida vence.
        Jobs deste processo (com tarefa viva) nunca sao tocados."""
        now = self._c.clock()
        failed = 0
        async with self._c.sessions.begin() as db:
            stmt = (
                select(BacktestJobRow)
                .where(BacktestJobRow.status == "running")
                .with_for_update(skip_locked=True)
            )
            for row in (await db.scalars(stmt)).all():
                beat = _parse((row.progress or {}).get("owner_beat"))
                if row.id in self._tasks or (
                    beat is not None and (now - beat).total_seconds() < OWNER_DEAD_AFTER_SECONDS
                ):
                    continue
                row.status = "failed"
                row.error = "Interrompido: a API reiniciou durante a execução. Rode de novo."
                row.finished_at = now
                failed += 1
        if failed:
            log.warning("backtest_zombies_failed", jobs=failed)
        return failed

    async def run_sweeper(self, interval: float = SWEEP_INTERVAL_SECONDS) -> None:
        while True:
            try:
                await self.sweep()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log.error("backtest_sweep_failed", error=type(error).__name__)
            await asyncio.sleep(interval)

    def _owned_elsewhere_and_alive(self, row: BacktestJobRow, now: datetime) -> bool:
        progress = row.progress or {}
        beat = _parse(progress.get("owner_beat"))
        if beat is None or progress.get("owner") == self._owner:
            return False
        return (now - beat).total_seconds() < OWNER_DEAD_AFTER_SECONDS

    def submit(self, job_id: str) -> None:
        task = asyncio.get_running_loop().create_task(self._run(job_id))
        self._tasks[job_id] = task
        task.add_done_callback(lambda _t, key=job_id: self._tasks.pop(key, None))  # type: ignore[misc]

    async def cancel(self, job_id: str) -> None:
        process = self._procs.get(job_id)
        if process is not None and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()

    async def shutdown(self) -> None:
        for process in list(self._procs.values()):
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
        for task in list(self._tasks.values()):
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    # ------------------------------------------------------------------ execução

    async def _run(self, job_id: str) -> None:
        async with self._slots:
            workdir = Path(tempfile.mkdtemp(prefix="ci-backtest-"))
            try:
                await self._execute(job_id, workdir)
            except asyncio.CancelledError:
                await self._finish(job_id, "failed", error="Interrompido: a API foi encerrada.")
                raise
            except Exception as error:
                log.error("backtest_runner_failed", job=job_id, error=type(error).__name__)
                await self._finish(job_id, "failed", error="Falha interna ao executar o backtest.")
            finally:
                shutil.rmtree(workdir, ignore_errors=True)

    async def _execute(self, job_id: str, workdir: Path) -> None:
        settings = self._c.settings
        async with self._c.sessions.begin() as db:
            row = await db.get(BacktestJobRow, job_id, with_for_update=True)
            if row is None or row.status != "queued":
                return
            if row.cancel_requested:
                row.status, row.finished_at = "cancelled", self._c.clock()
                return
            row.status, row.started_at = "running", self._c.clock()
            row.progress = {
                **(row.progress or {}),
                "owner": self._owner,
                "owner_beat": self._c.clock().isoformat(),
            }
            params = dict(row.params)
        params_file, out_file = workdir / "job.json", workdir / "result.json"
        params_file.write_text(json.dumps(params), encoding="utf-8")
        command = [
            sys.executable,
            "-m",
            "engine.runner.backtest_job",
            "--params",
            str(params_file),
            "--out",
            str(out_file),
            "--cache-dir",
            str(settings.backtest_cache_dir.resolve()),
            "--max-memory-mb",
            str(settings.backtest_max_memory_mb),
        ]
        if not settings.backtest_use_db:
            command.append("--no-db")
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=ROOT,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=1 << 20,
        )
        self._procs[job_id] = process
        error: str | None = None
        timed_out = False
        watcher = asyncio.create_task(self._watch_cancel(job_id, process))
        try:
            error = await asyncio.wait_for(self._consume(job_id, process), settings.backtest_timeout_seconds)
        except TimeoutError:
            timed_out = True
            with contextlib.suppress(ProcessLookupError):
                process.kill()
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            await process.wait()
            self._procs.pop(job_id, None)
        async with self._c.sessions() as db:
            cancelled = bool(
                (await db.get(BacktestJobRow, job_id, populate_existing=True) or row).cancel_requested
            )
        if cancelled:
            await self._finish(job_id, "cancelled")
        elif timed_out:
            await self._finish(job_id, "failed", error="Tempo máximo excedido: o backtest foi interrompido.")
        elif process.returncode == 0 and out_file.is_file():
            await self._finish(job_id, "succeeded", result=json.loads(out_file.read_text(encoding="utf-8")))
        else:
            await self._finish(job_id, "failed", error=error or "O backtest terminou com erro.")

    async def _consume(self, job_id: str, process: asyncio.subprocess.Process) -> str | None:
        assert process.stdout is not None  # noqa: S101
        last_write = 0.0
        error: str | None = None
        loop = asyncio.get_running_loop()
        async for raw in process.stdout:
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            kind = message.get("type")
            if kind == "error":
                error = scrub_text(str(message.get("message", "")))[:300]
                if message.get("code") == "memory_limit":
                    error = "Memória máxima excedida: o backtest foi interrompido."
            elif kind == "progress" and loop.time() - last_write >= 1.0:
                last_write = loop.time()
                await self._progress(job_id, message)
        await process.wait()
        return error

    async def _watch_cancel(self, job_id: str, process: asyncio.subprocess.Process) -> None:
        ticks = 0
        while process.returncode is None:
            await asyncio.sleep(1.0)
            ticks += 1
            async with self._c.sessions() as db:
                row = await db.get(BacktestJobRow, job_id, populate_existing=True)
                requested = row is not None and row.cancel_requested
            if ticks % int(OWNER_BEAT_SECONDS) == 0:
                await self._beat(job_id)
            if requested and process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()
                return

    async def _beat(self, job_id: str) -> None:
        async with self._c.sessions.begin() as db:
            row = await db.get(BacktestJobRow, job_id)
            if row is not None and row.status == "running":
                row.progress = {
                    **(row.progress or {}),
                    "owner": self._owner,
                    "owner_beat": self._c.clock().isoformat(),
                }

    async def _progress(self, job_id: str, message: dict[str, Any]) -> None:
        payload = {
            "stage": str(message.get("stage", "")),
            "percent": float(message.get("percent", 0)),
            "message": str(message.get("message", ""))[:200],
            "symbol": message.get("symbol"),
            "updated_at": self._c.clock().isoformat(),
        }
        async with self._c.sessions.begin() as db:
            row = await db.get(BacktestJobRow, job_id)
            if row is not None and row.status == "running":
                row.progress = {**(row.progress or {}), **payload}

    async def _finish(
        self, job_id: str, status: str, *, result: dict[str, Any] | None = None, error: str | None = None
    ) -> None:
        async with self._c.sessions.begin() as db:
            row = await db.get(BacktestJobRow, job_id, with_for_update=True)
            if row is None or row.status in ("succeeded", "failed", "cancelled"):
                return
            row.status = status
            row.finished_at = self._c.clock()
            row.error = error
            if result is not None:
                row.result = result
                row.progress = {
                    "stage": "done",
                    "percent": 100.0,
                    "message": "Concluído",
                    "symbol": None,
                    "updated_at": self._c.clock().isoformat(),
                }


class BacktestService:
    def __init__(self, container: Container, audit: AuditService | None = None) -> None:
        self._c = container
        self._audit = audit or AuditService(container.sessions)

    async def create(self, body: BacktestCreate, *, actor: str, meta: RequestMeta) -> BacktestJobOut:
        now = self._c.clock()
        params = resolve_params(body, now, self._c.settings.backtest_max_years)
        async with self._c.sessions.begin() as db:
            active = (
                await db.scalars(select(BacktestJobRow.id).where(BacktestJobRow.status.in_(ACTIVE)).limit(20))
            ).all()
            if len(active) >= 10:
                raise Conflict("Há backtests demais na fila. Espere alguns terminarem ou cancele algum.")
            row = BacktestJobRow(
                id=uuid.uuid4().hex,
                created_by=actor[:100],
                status="queued",
                params=params,
                progress={
                    "stage": "queued",
                    "percent": 0.0,
                    "message": "Na fila",
                    "symbol": None,
                    "updated_at": now.isoformat(),
                },
                cancel_requested=False,
                created_at=now,
            )
            db.add(row)
            await self._audit.record(
                actor=actor,
                action=AuditAction.BACKTEST_CREATED,
                target=f"backtest:{row.id}",
                result=AuditResult.SUCCESS,
                meta=meta,
                details={
                    "symbols": params["symbols"],
                    "timeframe": params["timeframe"],
                    "walk_forward": params["walk_forward"],
                    "days": params["days"],
                },
                session=db,
            )
        if self._c.backtests is not None:
            self._c.backtests.submit(row.id)
        return job_out(row)

    async def get(self, job_id: str) -> BacktestJobRow:
        async with self._c.sessions() as db:
            row = await db.get(BacktestJobRow, job_id)
        if row is None:
            raise NotFound("Backtest não encontrado.")
        return row

    async def list(self, *, limit: int, cursor: str | None) -> BacktestPage:
        stmt = select(BacktestJobRow).order_by(BacktestJobRow.created_at.desc(), BacktestJobRow.id.desc())
        if cursor:
            after = decode_cursor(cursor)
            moment, last_id = after.moment, after.last_id
            stmt = stmt.where(
                (BacktestJobRow.created_at < moment)
                | ((BacktestJobRow.created_at == moment) & (BacktestJobRow.id < last_id))
            )
        async with self._c.sessions() as db:
            rows = list((await db.scalars(stmt.limit(limit + 1))).all())
        page = rows[:limit]
        nxt = encode_cursor(page[-1].created_at, page[-1].id) if len(rows) > limit else None
        return BacktestPage(items=[job_out(r) for r in page], next_cursor=nxt)

    async def report(self, job_id: str) -> BacktestReportOut:
        row = await self.get(job_id)
        if row.status in ACTIVE:
            raise Conflict("O backtest ainda está rodando. Acompanhe o progresso e volte depois.")
        if row.result is None:
            raise Conflict(row.error or "Este backtest não gerou relatório.")
        return BacktestReportOut(
            job_id=row.id,
            generated_at=_parse(row.result.get("generated_at")),
            disclaimer=DISCLAIMER,
            assumptions=row.result.get("assumptions", {}),
            results=row.result.get("results", []),
        )

    async def cancel(self, job_id: str, *, actor: str, meta: RequestMeta) -> BacktestJobOut:
        async with self._c.sessions.begin() as db:
            row = await db.get(BacktestJobRow, job_id, with_for_update=True)
            if row is None:
                raise NotFound("Backtest não encontrado.")
            if row.status not in ACTIVE:
                raise Conflict("Este backtest já terminou.")
            row.cancel_requested = True
            if row.status == "queued" and job_id not in (
                self._c.backtests._tasks if self._c.backtests else {}
            ):
                row.status, row.finished_at = "cancelled", self._c.clock()
            await self._audit.record(
                actor=actor,
                action=AuditAction.BACKTEST_CANCELLED,
                target=f"backtest:{job_id}",
                result=AuditResult.SUCCESS,
                meta=meta,
                session=db,
            )
        if self._c.backtests is not None:
            await self._c.backtests.cancel(job_id)
        return job_out(await self.get(job_id))
