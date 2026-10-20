"""Reverificacao 2 (QA): matriz de queda do processo em CADA gravacao do diario, em Postgres real descartavel.

Cenario: sinal (vela 29), fill da entrada (30) e stop (31). Para cada (metodo, n-esima chamada) o processo
"morre" ANTES de gravar ou DEPOIS do commit; um novo robo reabre o mesmo run e termina o cenario. Invariantes:
exatamente 1 trade, 2 fills, nenhuma posicao aberta, patrimonio igual ao cenario sem queda.
"""

import asyncio
import contextlib
from collections import Counter
from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from engine.domain.candle import Candle
from engine.infrastructure.config.settings import Settings
from engine.infrastructure.persistence.session import create_engine, create_session_factory
from engine.runner.paper_journal import SqlJournal
from tests.integration.disposable_db import (
    assert_disposable_database,
    disposable_database,
    upgrade_database,
)
from tests.unit.runner.fakes import FakeFeed, Harness, SimulatedCrash, candle_at

pytestmark = pytest.mark.integration

SIGNAL = ("100", "100.5", "99.5", "100")
FILL = ("100", "101", "99.5", "100.5")
STOP = ("100.5", "100.6", "94", "95")
METHODS = (
    "record_decision",
    "update_order",
    "record_entry",
    "record_exit",
    "record_position",
    "record_control",
    "save_state",
    "record_equity",
)


class CrashingJournal(SqlJournal):
    target: tuple[str, int, str] | None = None
    counts: Counter[str]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.counts = Counter()


def _wrap(name: str) -> Any:
    original = getattr(SqlJournal, name)

    async def wrapper(self: CrashingJournal, *args: Any, **kwargs: Any) -> Any:
        self.counts[name] += 1
        hit = self.target is not None and self.target[:2] == (name, self.counts[name])
        if hit and self.target is not None and self.target[2] == "before":
            raise SimulatedCrash(f"{name}#{self.counts[name]} antes")
        result = await original(self, *args, **kwargs)
        if hit and self.target is not None and self.target[2] == "ambiguous":
            raise OperationalError(name, {}, ConnectionResetError("conexao caiu depois do commit"))
        if hit:
            raise SimulatedCrash(f"{name}#{self.counts[name]} depois")
        return result

    return wrapper


for _method in METHODS:
    setattr(CrashingJournal, _method, _wrap(_method))


@contextlib.asynccontextmanager
async def _migrated_db(settings: Settings) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Banco `*_test` migrado e exclusivo (criado/derrubado em thread: as ajudas usam asyncio.run)."""
    manager = disposable_database(settings, "crashmx")
    url = await asyncio.to_thread(manager.__enter__)
    try:
        assert_disposable_database(url)
        await asyncio.to_thread(upgrade_database, url)
        engine: AsyncEngine = create_engine(url)
        try:
            yield create_session_factory(engine)
        finally:
            await engine.dispose()
    finally:
        await asyncio.to_thread(manager.__exit__, None, None, None)


class Lab:
    """Um banco por teste; cenarios que deixam run HALTED (herdado pelo seguinte) pedem banco proprio."""

    def __init__(self, settings: Settings, shared: async_sessionmaker[AsyncSession]) -> None:
        self.settings = settings
        self.shared = shared

    def fresh(self) -> contextlib.AbstractAsyncContextManager[async_sessionmaker[AsyncSession]]:
        return _migrated_db(self.settings)

    @contextlib.asynccontextmanager
    async def for_script(self, script: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
        if script == "halt":
            async with self.fresh() as sessions:
                yield sessions
        else:
            yield self.shared


@pytest_asyncio.fixture
async def lab(settings: Settings, db_ready: None) -> AsyncIterator[Lab]:
    async with _migrated_db(settings) as sessions:
        yield Lab(settings, sessions)


@pytest_asyncio.fixture
async def sessions_iso(lab: Lab) -> async_sessionmaker[AsyncSession]:
    return lab.shared


@dataclass
class Outcome:
    trades: list[tuple[str, Decimal]]
    fills: int
    open_positions: int
    equity: Decimal
    ledger: int
    live: list[str]
    paused: bool
    halted: bool
    halts_db: int
    pause_reason: str | None
    gateway: str
    controls: int
    counts: Counter[str]


SCRIPTS = {
    "stop": (candle_at(31, *STOP), "94.9", "95.1", "stop_loss"),
    "profit": (candle_at(31, "100.5", "108", "100.4", "104"), "103.9", "104.1", "profit_protection"),
    "halt": (candle_at(31, *STOP), "94.9", "95.1", "stop_loss"),
    "samebar": (None, "94.9", "95.1", "stop_loss"),
}


async def _scenario(lab: Lab, target: tuple[str, int, str] | None, script: str = "stop") -> Outcome:
    async with lab.for_script(script) as sessions:
        return await _scenario_in(sessions, target, script)


async def _scenario_in(
    sessions: async_sessionmaker[AsyncSession], target: tuple[str, int, str] | None, script: str = "stop"
) -> Outcome:
    last_candle, bid, ask, _reason = SCRIPTS[script]
    run_id = f"mx-{uuid4().hex[:12]}"
    journal = CrashingJournal(sessions, run_id)
    journal.target = target
    candles: list[Candle] = (
        [candle_at(29, *SIGNAL), candle_at(30, "100", "101", "94", "95")]
        if last_candle is None
        else [candle_at(29, *SIGNAL), candle_at(30, *FILL), last_candle]
    )
    h = Harness(journal=journal)
    delivered = 0
    crashed = False
    try:
        await h.start()
        for candle in candles:
            if script == "halt" and candle is candles[-1]:
                h.robot.state = h.robot.state.model_copy(update={"peak_equity": Decimal(10560)})
                await h.robot._save_state(h.clock.now())
            await h.close(candle)
            delivered += 1
        for _ in range(3):
            h.clock.advance(seconds=1)
            await h.tick(bid, ask)
    except SimulatedCrash:
        crashed = True
    robot_h = h
    if crashed:
        plain = SqlJournal(sessions, run_id)
        known = [*h.history, *candles[: delivered + 1]]
        robot_h = await Harness(journal=plain, history=h.history, feed=FakeFeed(known), clock=h.clock).start()
        if script == "halt" and not robot_h.robot.state.halted:
            robot_h.robot.state = robot_h.robot.state.model_copy(update={"peak_equity": Decimal(10560)})
            await robot_h.robot._save_state(robot_h.clock.now())
        last = robot_h.robot._last_handled.get("BTC/USDT")
        for candle in candles:
            if last is None or candle.open_time > last:
                await robot_h.close(candle)
        for _ in range(3):
            robot_h.clock.advance(seconds=1)
            await robot_h.tick(bid, ask)
    async with sessions() as session:
        trades = (
            await session.execute(text("select exit_reason, pnl from trades where run_id=:r"), {"r": run_id})
        ).all()
        fills = await session.scalar(text("select count(*) from fills where run_id=:r"), {"r": run_id})
        opened = await session.scalar(
            text("select count(*) from positions where run_id=:r and closed_at is null"), {"r": run_id}
        )
        controls = await session.scalar(
            text("select count(*) from control_events where run_id=:r"), {"r": run_id}
        )
        halts = await session.scalar(
            text("select count(*) from control_events where run_id=:r and action='halt'"), {"r": run_id}
        )
    state = robot_h.robot.state
    return Outcome(
        trades=[(str(r[0]), Decimal(r[1])) for r in trades],
        fills=int(fills or 0),
        open_positions=int(opened or 0),
        equity=state.equity,
        ledger=len(state.ledger),
        live=robot_h.robot.live_symbols,
        paused=state.paused,
        halted=state.halted,
        halts_db=int(halts or 0),
        pause_reason=state.pause_reason,
        gateway=repr(robot_h.robot.gateway.snapshot().orders),
        controls=int(controls or 0),
        counts=journal.counts,
    )


async def _cases(lab: Lab, script: str) -> list[tuple[str, int, str]]:
    base = await _scenario(lab, None, script)
    return [
        (m, n, mode)
        for m, total in base.counts.items()
        for n in range(1, total + 1)
        for mode in ("before", "after")
    ]


@pytest.mark.parametrize("script", ["stop", "profit", "samebar", "halt"])
async def test_crash_matrix_every_journal_write_before_and_after_commit(lab: Lab, script: str) -> None:
    baseline = await _scenario(lab, None, script)
    assert len(baseline.trades) == 1 and baseline.fills == 2 and baseline.open_positions == 0
    failures: list[str] = []
    lost: list[tuple[str, int, str]] = []
    for case in await _cases(lab, script):
        try:
            out = await _scenario(lab, case, script)
        except Exception as error:  # a recuperacao quebrou: tambem e uma falha
            failures.append(f"{case}: recuperacao levantou {error!r}")
            continue
        full = (
            [r for r, _ in out.trades] == [SCRIPTS[script][3]]
            and out.fills == 2
            and out.ledger == 1
            and out.equity == Decimal(10000) + out.trades[0][1]
        )
        # sinal perdido na queda (vela velha nao entra) e aceitavel: nada de ordem, fill, trade ou posicao
        none = not out.trades and out.fills == 0 and out.equity == Decimal(10000) and out.ledger == 0
        halt_ok = (not out.trades) or (out.halted and out.halts_db == 1) if script == "halt" else True
        paused = out.paused and not (script == "halt" and out.halted)
        if not (full or none) or out.open_positions or out.live or paused or not halt_ok:
            failures.append(f"{case}: {out}")
        elif none:
            lost.append(case)
    print("SINAL_PERDIDO", lost)
    assert not failures, "\n".join(failures)


@pytest.mark.parametrize("script", ["stop", "profit", "samebar", "halt"])
async def test_ambiguous_commit_retried_in_process_keeps_one_trade_and_no_duplicate_events(
    lab: Lab, script: str
) -> None:
    baseline = await _scenario(lab, None, script)
    failures: list[str] = []
    for method, total in baseline.counts.items():
        for n in range(1, total + 1):
            out = await _scenario(lab, (method, n, "ambiguous"), script)
            ok = (
                [r for r, _ in out.trades] == [SCRIPTS[script][3]]
                and out.fills == 2
                and out.ledger == 1
                and not out.open_positions
                and not out.live
                and not out.paused
                and out.controls == baseline.controls
            )
            if not ok:
                failures.append(f"{(method, n)}: {out} (controles esperados {baseline.controls})")
    assert not failures, chr(10).join(failures)


# ---------------------------------------------------------------- outbox x kill x restart (Postgres real)


class _Flaky(SqlJournal):
    down = False


def _flaky(name: str) -> Any:
    original = getattr(SqlJournal, name)

    async def wrapper(self: _Flaky, *args: Any, **kwargs: Any) -> Any:
        if self.down:
            raise OperationalError(name, {}, ConnectionResetError("conexao perdida"))
        return await original(self, *args, **kwargs)

    return wrapper


for _method in METHODS:
    setattr(_Flaky, _method, _flaky(_method))


async def _outage_with_queued_trade(
    sessions: async_sessionmaker[AsyncSession], tmp_path: Any
) -> tuple[Harness, _Flaky]:
    from engine.runner.paper_config import RunPaths

    journal = _Flaky(sessions, f"ob-{uuid4().hex[:12]}")
    paths = RunPaths(tmp_path, "r")
    paths.ensure()
    h = await Harness(journal=journal, paths=paths).start()
    await h.close(candle_at(29, *SIGNAL))
    journal.down = True
    await h.close(candle_at(30, *FILL))
    await h.close(candle_at(31, *STOP))
    assert h.robot.journal.pending_events >= 2
    return h, journal


async def _db_summary(sessions: async_sessionmaker[AsyncSession], run_id: str) -> tuple[int, int, int, int]:
    async with sessions() as session:
        counts = []
        for table in ("trades", "fills", "control_events"):
            counts.append(
                int(
                    await session.scalar(text(f"select count(*) from {table} where run_id=:r"), {"r": run_id})
                    or 0
                )
            )
        counts.append(
            int(
                await session.scalar(
                    text("select count(*) from positions where run_id=:r and closed_at is null"),
                    {"r": run_id},
                )
                or 0
            )
        )
    return counts[0], counts[1], counts[2], counts[3]


async def test_kill_during_outage_then_restart_reapplies_outbox_exactly_once(
    sessions_iso: async_sessionmaker[AsyncSession], tmp_path: Any
) -> None:
    from engine.runner.paper_config import RunPaths

    h, journal = await _outage_with_queued_trade(sessions_iso, tmp_path)
    journal.down = False  # o banco volta; o processo antigo morreu sem shutdown

    revived = await Harness(
        journal=journal,
        history=h.history,
        feed=FakeFeed(h.feed.candles),
        clock=h.clock,
        paths=RunPaths(tmp_path, "r"),
    ).start()

    trades, fills, _controls, opened = await _db_summary(sessions_iso, journal.run_id)
    assert (trades, fills, opened) == (1, 2, 0)
    assert revived.robot.journal.pending_events == 0
    assert len(revived.robot.state.ledger) == 1 and revived.robot.live_symbols == []
    assert not revived.robot.state.paused


async def test_crash_after_apply_before_ack_is_reapplied_idempotently(
    sessions_iso: async_sessionmaker[AsyncSession], tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from engine.runner import paper_outbox
    from engine.runner.paper_config import RunPaths

    h, journal = await _outage_with_queued_trade(sessions_iso, tmp_path)
    journal.down = False
    real_done = paper_outbox.Outbox.done
    calls = {"n": 0}

    def dying_done(self: Any, key: str) -> None:
        calls["n"] += 1
        if calls["n"] == 3:
            raise SimulatedCrash("morreu entre o commit e o ack do outbox")
        real_done(self, key)

    monkeypatch.setattr(paper_outbox.Outbox, "done", dying_done)
    with pytest.raises(SimulatedCrash):
        await Harness(
            journal=journal,
            history=h.history,
            feed=FakeFeed(h.feed.candles),
            clock=h.clock,
            paths=RunPaths(tmp_path, "r"),
        ).start()
    monkeypatch.setattr(paper_outbox.Outbox, "done", real_done)

    revived = await Harness(
        journal=journal,
        history=h.history,
        feed=FakeFeed(h.feed.candles),
        clock=h.clock,
        paths=RunPaths(tmp_path, "r"),
    ).start()

    trades, fills, _c, opened = await _db_summary(sessions_iso, journal.run_id)
    assert (trades, fills, opened) == (1, 2, 0)
    assert len(revived.robot.state.ledger) == 1 and not revived.robot.state.paused


async def test_corrupted_outbox_file_blocks_start_instead_of_silently_dropping_the_event(
    sessions_iso: async_sessionmaker[AsyncSession], tmp_path: Any
) -> None:
    from engine.runner.paper_config import RunPaths

    h, journal = await _outage_with_queued_trade(sessions_iso, tmp_path)
    journal.down = False
    victim = sorted((tmp_path / "r" / "outbox").glob("*.json"))[0]
    victim.write_text(victim.read_text(encoding="utf-8")[:40], encoding="utf-8")  # arquivo truncado

    with pytest.raises(Exception):  # noqa: B017,PT011  qualquer falha ruidosa serve; o que nao pode e iniciar
        await Harness(
            journal=journal,
            history=h.history,
            feed=FakeFeed(h.feed.candles),
            clock=h.clock,
            paths=RunPaths(tmp_path, "r"),
        ).start()
    trades, _fills, _c, _o = await _db_summary(sessions_iso, journal.run_id)
    assert trades == 0  # nada foi aplicado fora de ordem


async def test_halt_must_survive_an_intermediate_run_that_died_before_inheriting(
    sessions_iso: async_sessionmaker[AsyncSession],
) -> None:
    from datetime import UTC, datetime, timedelta

    from engine.application.risk import RiskState
    from engine.domain.journal import ControlAction, ControlEvent, RunMode, RunRecord

    # Arrange
    t0 = datetime(2026, 1, 1, tzinfo=UTC)

    def run(name: str, at: datetime) -> RunRecord:
        return RunRecord(run_id=name, mode=RunMode.PAPER, started_at=at, config_hash="h", code_version="v")

    def start(at: datetime) -> ControlEvent:
        return ControlEvent(at=at, action=ControlAction.START, operator="system", reason="inicio")

    halted = RiskState.initial(Decimal(10000), t0).model_copy(update={"halted": True, "halt_reason": "dd"})
    first = SqlJournal(sessions_iso, "run-a")
    await first.start_run(run("run-a", t0), {"config": {}}, start(t0))
    await first.record_control(
        ControlEvent(at=t0, action=ControlAction.HALT, operator="system", reason="dd"), risk_state=halted
    )
    later = t0 + timedelta(hours=1)
    await SqlJournal(sessions_iso, "run-b").start_run(run("run-b", later), {"config": {}}, start(later))

    # Act
    inherited = await SqlJournal(sessions_iso, "run-c").previous_halt()

    # Assert
    assert inherited is not None
