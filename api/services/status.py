"""Status do robô para o painel: estado, pausas, travas e elegibilidade ao Live, num só objeto."""

from datetime import date
from decimal import Decimal
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession

from api.container import Container
from api.reasons import persistence_text
from api.schemas_trading import BotStateName, BotStatusOut, PauseReasonOut
from api.services.limits import build_limits, state_words
from api.services.livegate import LiveGateService, eligibility_of
from api.services.queries import as_float, latest_run, open_position_rows, pending_commands
from api.services.runner_status import RunnerSnapshot, RunnerStatusService, parse_time
from engine.domain.risk import RiskConfig
from engine.infrastructure.persistence.config_repository import ConfigRepository
from engine.infrastructure.persistence.state_repository import RiskStateRepository

KNOWN_STATES = {"STARTING", "RUNNING", "PAUSED", "FAILED_PERSISTENCE", "HALTED", "STOPPING", "STOPPED"}


async def db_view(db: AsyncSession, run_id: str | None) -> dict[str, Any]:
    """Visão mínima vinda do banco quando o runner não publica nada (processo parado ou Redis fora)."""
    if run_id is None:
        return {}
    state = await RiskStateRepository(db).load_latest(run_id)
    positions = [p.symbol for p in await open_position_rows(db, run_id)]
    if state is None:
        return {"run_id": run_id, "open_positions": positions}
    return {
        "run_id": run_id,
        "equity": str(state.equity),
        "realized_equity": str(state.equity),
        "peak_equity": str(state.peak_equity),
        "drawdown": str(state.drawdown),
        "day": state.day.isoformat(),
        "day_pnl": str(state.day_pnl),
        "day_start_equity": str(state.day_start_equity),
        "daily_pause_until": state.daily_pause_until.isoformat() if state.daily_pause_until else None,
        "target_reached_on": state.target_reached_on.isoformat() if state.target_reached_on else None,
        "loss_streaks": dict(state.loss_streaks),
        "pair_pause_until": {s: t.isoformat() for s, t in state.pair_pause_until.items()},
        "halted": state.halted,
        "halt_reason": state.halt_reason,
        "paused": state.paused,
        "pause_reason": state.pause_reason,
        "open_positions": positions,
    }


def _pauses(view: dict[str, Any]) -> list[PauseReasonOut]:
    found: list[PauseReasonOut] = []
    for entry in view.get("pause_reasons") or []:
        at = parse_time(entry.get("at"))
        if at is not None:
            found.append(
                PauseReasonOut(reason=str(entry.get("reason")), operator=str(entry.get("operator")), at=at)
            )
    return found


def _date(raw: object) -> date | None:
    try:
        return date.fromisoformat(str(raw)) if raw else None
    except ValueError:
        return None


async def current_run_id(container: Container) -> str | None:
    """Run em foco: o que o robô diz estar rodando ou, sem sinal, o paper mais recente do diario."""
    snapshot = await RunnerStatusService(container).snapshot()
    async with container.sessions() as db:
        run = await latest_run(db, snapshot.run_id)
    return run.run_id if run is not None else snapshot.run_id


class StatusService:
    def __init__(self, container: Container) -> None:
        self._c = container

    async def snapshot(self) -> RunnerSnapshot:
        return await RunnerStatusService(self._c).snapshot()

    async def build(self) -> BotStatusOut:
        c = self._c
        snapshot = await self.snapshot()
        async with c.sessions() as db:
            run = await latest_run(db, snapshot.run_id)
            run_id = run.run_id if run is not None else snapshot.run_id
            view = dict(snapshot.data) if snapshot.data is not None else await db_view(db, run_id)
            pending = await pending_commands(db)
            latest = await ConfigRepository(db).latest()
        evaluation = await LiveGateService(c).evaluate(run_id)
        state = snapshot.state if snapshot.state in KNOWN_STATES else None
        if state is None:
            state = (
                "STOPPED"
                if run is not None and run.ended_at is not None and snapshot.data is None
                else "OFFLINE"
            )
        label, detail = state_words(state)
        if state in {"RUNNING", "PAUSED"} and snapshot.connection == "stale":
            label, detail = state_words("OFFLINE")
        risk = _risk(view, latest.document.risk if latest else None)
        now = c.clock()
        day_start = Decimal(str(view.get("day_start_equity") or 0))
        day_pnl = as_float(view.get("day_pnl"))
        drawdown = as_float(view.get("drawdown"))
        return BotStatusOut(
            run_id=run_id,
            mode="paper",
            state=cast("BotStateName", state),
            state_label=label,
            state_detail=detail,
            connection=snapshot.connection,
            heartbeat_at=snapshot.at,
            heartbeat_age_seconds=None if snapshot.age_seconds is None else round(snapshot.age_seconds, 1),
            started_at=snapshot.started_at or (run.started_at if run is not None else None),
            code_version=view.get("code_version"),
            stops_blind=bool(view.get("stops_blind")),
            halted=bool(view.get("halted")),
            halt_reason=view.get("halt_reason"),
            paused=bool(view.get("paused")),
            pause_reasons=_pauses(view),
            outbox_pending=int(view.get("outbox_pending") or 0),
            persistence_error=persistence_text(view.get("persistence_error")),
            redis_healthy=view.get("redis_healthy"),
            allow_open_calendar=bool(view.get("allow_open_calendar")),
            open_positions=[str(s) for s in view.get("open_positions") or []],
            pending_entries=[str(s) for s in view.get("pending_entries") or []],
            equity=as_float(view.get("equity")),
            realized_equity=as_float(view.get("realized_equity")),
            peak_equity=as_float(view.get("peak_equity")),
            drawdown_pct=None if drawdown is None else round(drawdown * 100, 4),
            day_pnl=day_pnl,
            day_pnl_pct=None
            if day_pnl is None or day_start <= 0
            else round(day_pnl / float(day_start) * 100, 4),
            daily_pause_until=parse_time(view.get("daily_pause_until")),
            target_reached_on=_date(view.get("target_reached_on")),
            pair_pause_until={
                s: t
                for s, raw in (view.get("pair_pause_until") or {}).items()
                if (t := parse_time(raw)) is not None
            },
            config_hash=view.get("config_hash"),
            config_version=view.get("config_version"),
            simulated=True,
            live=eligibility_of(evaluation),
            limits=build_limits(view, risk, now),
            pending_commands=pending,
        )


def _risk(view: dict[str, Any], fallback: RiskConfig | None) -> RiskConfig:
    raw = view.get("risk")
    if isinstance(raw, dict):
        try:
            return RiskConfig.model_validate(raw)
        except ValueError:
            pass
    return fallback or RiskConfig()
