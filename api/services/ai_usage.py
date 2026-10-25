from typing import Any

from api.container import Container
from api.schemas_trading import AiUsageOut
from api.services.runner_status import RunnerStatusService
from engine.application.ai.ledger import month_start
from engine.infrastructure.persistence.ai_call_repository import AiCallRepository
from engine.infrastructure.persistence.config_repository import ConfigRepository


class AiUsageService:
    def __init__(self, container: Container) -> None:
        self._c = container

    async def build(self) -> AiUsageOut:
        start = month_start(self._c.clock())
        async with self._c.sessions() as db:
            usage = await AiCallRepository(db).usage(start)
            latest = await ConfigRepository(db).latest()
        snapshot = await RunnerStatusService(self._c).snapshot()
        raw = None if not snapshot.data else snapshot.data.get("ai")
        runner: dict[str, Any] = raw if isinstance(raw, dict) else {}
        by = usage.by_result
        return AiUsageOut(
            month_start=start,
            spent_usd=float(usage.spent_usd),
            budget_usd=None if latest is None else float(latest.document.ai.monthly_budget_usd),
            calls=usage.calls,
            failures=sum(by.get(k, 0) for k in ("invalid", "timeout", "error")),
            budget_stops=by.get("budget", 0),
            by_result=by,
            runner_status=runner.get("status"),
            circuit_open=runner.get("circuit_open"),
            calls_last_hour=runner.get("calls_last_hour"),
        )
