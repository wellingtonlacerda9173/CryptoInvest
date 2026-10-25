import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from engine.application.ai import AnalystOpinion, AnalystRequest
from engine.application.ai.ledger import AiCallRecord, AiUsage, month_start
from engine.application.ai.models import (
    CandleView,
    IndicatorsView,
    SignalView,
)
from engine.infrastructure.ai.client import LlmReply

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)


def make_request(
    strategy: str = "elephant", confluence: str = "volume forte", close: float = 100.0
) -> AnalystRequest:
    return AnalystRequest(
        symbol="BTC/USDT",
        timeframe="1h",
        regime="lateral",
        candles=tuple(
            CandleView(ago=2 - i, open=close, high=close + 1, low=close - 1, close=close, volume=10.0)
            for i in range(3)
        ),
        indicators=IndicatorsView(rsi14=55.0),
        signal=SignalView(
            strategy=strategy,
            entry=100.0,
            stop=98.0,
            target=104.0,
            reward_risk=2.0,
            confidence=0.8,
            confluences=(confluence,),
        ),
    )


def opinion(verdict: str = "approve", confidence: float = 0.9, reason: str = "ok") -> AnalystOpinion:
    return AnalystOpinion(
        verdict=verdict,  # type: ignore[arg-type]
        confidence=confidence,
        reason=reason,
        model="fake",
        prompt_version="analyst-v1",
    )


def reply_json(verdict: str = "approve", confidence: float = 0.7, reason: str = "setup coerente") -> str:
    return json.dumps({"verdict": verdict, "confidence": confidence, "reason": reason})


class FakeStore:
    def __init__(self, spent: Decimal = Decimal(0), fail_usage: bool = False) -> None:
        self.calls: list[AiCallRecord] = []
        self.spent = spent
        self.fail_usage = fail_usage
        self.usage_loads = 0

    async def record(self, call: AiCallRecord) -> None:
        self.calls.append(call)

    async def usage(self, start: datetime) -> AiUsage:
        self.usage_loads += 1
        if self.fail_usage:
            raise RuntimeError("banco fora")
        spent = self.spent + sum((c.cost_usd for c in self.calls), Decimal(0))
        return AiUsage(month_start=start, spent_usd=spent, calls=0, by_result={})


class FakeClient:
    """Responde com o que `answer` devolver (ou levanta); guarda os pedidos."""

    def __init__(self, answer: Callable[[str], LlmReply | Exception]) -> None:
        self.answer = answer
        self.requests: list[dict[str, object]] = []

    async def complete(
        self, *, model: str, system: str, user: str, max_tokens: int, deadline_seconds: float
    ) -> LlmReply:
        self.requests.append(
            {"model": model, "system": system, "user": user, "max_tokens": max_tokens, "t": deadline_seconds}
        )
        result = self.answer(user)
        if isinstance(result, Exception):
            raise result
        return result


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += timedelta(**kw)


async def no_sleep(_: float) -> None:
    return None


def good(text: str | None = None, tokens: tuple[int, int] = (1500, 120)) -> LlmReply:
    return LlmReply(text or reply_json(), *tokens)


MONTH = month_start(NOW)
