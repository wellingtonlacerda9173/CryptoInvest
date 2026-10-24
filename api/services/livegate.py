"""Trava do Live em leitura: critérios (valor x meta) avaliados sobre o diário do run. Nunca ativa nada."""

import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

import structlog

from api.container import Container
from api.schemas_control import LIVE_PHRASE, LIVE_UNAVAILABLE
from api.schemas_trading import GateCriterionOut, LiveEligibilityOut, LiveGateOut
from api.services.fmt import number, pct
from engine.application.paper_metrics import (
    LiveGateReport,
    LiveGateThresholds,
    PaperMetrics,
    compute_paper_metrics,
    evaluate_live_gate,
)
from engine.domain.risk import RiskConfig
from engine.runner.paper_report import INELIGIBLE_OPEN_CALENDAR, ReportInputs, live_verdict
from engine.runner.paper_report_data import UnknownRunError, collect_inputs

log = structlog.get_logger()

GATE_TTL_SECONDS: Final = 30.0
FIRST_ACTIVATION_REDUCED_DAYS: Final = 7
LIVE_AVAILABLE: Final = False
NO_TEST_CODE: Final = "sem teste"
GATE_CACHE_SIZE: Final = 64
NO_TEST_TEXT = (
    "Sem teste: ainda não há um teste no Paper com diário para avaliar. Sem teste, nada é liberado para "
    "dinheiro real."
)
INELIGIBLE_TEXT = (
    "Este teste rodou sem o calendário de eventos. Ele nunca vai poder liberar dinheiro real; "
    "é preciso começar um novo teste com o calendário."
)


@dataclass(frozen=True, slots=True)
class GateEvaluation:
    run_id: str | None
    report: LiveGateReport
    thresholds: LiveGateThresholds
    ineligible_reason: str | None
    inputs: ReportInputs | None

    @property
    def eligible(self) -> bool:
        return self.ineligible_reason is None

    @property
    def no_test(self) -> bool:
        return self.ineligible_reason == NO_TEST_CODE

    @property
    def reason_text(self) -> str | None:
        if self.ineligible_reason is None:
            return None
        return NO_TEST_TEXT if self.no_test else INELIGIBLE_TEXT


def _f(value: Decimal | None, scale: int = 100) -> float | None:
    return None if value is None else float(value * scale)


def criteria_of(evaluation: GateEvaluation) -> list[GateCriterionOut]:
    metrics: PaperMetrics = evaluation.report.metrics
    t = evaluation.thresholds
    by_code = {c.code: c for c in evaluation.report.criteria}
    days = metrics.operating_days
    drawdown = metrics.max_drawdown
    factor = metrics.profit_factor
    pf = by_code["profit_factor"]
    rows = [
        GateCriterionOut(
            code="operating_days",
            name=f"Pelo menos {t.min_days} dias de teste no Paper",
            status="met" if by_code["operating_days"].met else "in_progress",
            value_text=f"{days} de {t.min_days}",
            target_text=f"{t.min_days} dias",
            value=float(days),
            target=float(t.min_days),
            missing_text=None if days >= t.min_days else f"Faltam {t.min_days - days} dias",
        ),
        GateCriterionOut(
            code="max_drawdown",
            name=f"Drawdown máximo abaixo de {pct(t.max_drawdown * 100, 0)}",
            status="met"
            if by_code["max_drawdown"].met
            else ("in_progress" if drawdown is None else "failed"),
            value_text="Não medido ainda" if drawdown is None else pct(drawdown * 100),
            target_text=f"abaixo de {pct(t.max_drawdown * 100, 0)}",
            value=_f(drawdown),
            target=_f(t.max_drawdown),
            reason=by_code["max_drawdown"].reason,
        ),
        GateCriterionOut(
            code="profit_factor",
            name=f"Profit factor acima de {number(t.min_profit_factor, 1)}",
            status="met" if pf.met else ("in_progress" if pf.reason else "failed"),
            value_text="Sem perdas para medir" if factor is None else number(factor),
            target_text=f"acima de {number(t.min_profit_factor, 1)}",
            value=_f(factor, 1),
            target=float(t.min_profit_factor),
            reason=pf.reason,
        ),
        GateCriterionOut(
            code="min_trades",
            name=f"Pelo menos {t.min_trades} operações",
            status="met" if metrics.trades >= t.min_trades else "in_progress",
            value_text=f"{metrics.trades} de {t.min_trades}",
            target_text=f"{t.min_trades} operações",
            value=float(metrics.trades),
            target=float(t.min_trades),
            missing_text=None
            if metrics.trades >= t.min_trades
            else f"Faltam {t.min_trades - metrics.trades}",
        ),
        GateCriterionOut(
            code="min_losses",
            name=f"Pelo menos {t.min_losses} perdas na amostra (sem perdas não dá para medir o risco)",
            status="met" if metrics.losing_trades >= t.min_losses else "in_progress",
            value_text=f"{metrics.losing_trades} de {t.min_losses}",
            target_text=f"{t.min_losses} perdas",
            value=float(metrics.losing_trades),
            target=float(t.min_losses),
        ),
        GateCriterionOut(
            code="daily_loss",
            name=f"Perda diária nunca acima de {pct(t.max_daily_loss * 100, 1)}",
            status="met" if by_code["daily_loss"].met else "failed",
            value_text=pct(metrics.worst_daily_loss_pct * 100),
            target_text=f"até {pct(t.max_daily_loss * 100, 1)}",
            value=_f(metrics.worst_daily_loss_pct),
            target=_f(t.max_daily_loss),
        ),
        GateCriterionOut(
            code="eligible_for_live",
            name="Run elegível (calendário de eventos ligado o tempo todo)",
            status="met" if evaluation.eligible else "failed",
            value_text="Elegível" if evaluation.eligible else "Inelegível",
            target_text="Elegível",
            value=None,
            target=None,
            reason=evaluation.reason_text,
        ),
    ]
    return rows


def eligibility_of(evaluation: GateEvaluation) -> LiveEligibilityOut:
    rows = criteria_of(evaluation)
    return LiveEligibilityOut(
        eligible=evaluation.eligible,
        approved=evaluation.report.approved,
        reason=evaluation.reason_text,
        criteria_met=sum(r.status == "met" for r in rows),
        criteria_total=len(rows),
        live_available=LIVE_AVAILABLE,
    )


def gate_out(evaluation: GateEvaluation) -> LiveGateOut:
    verdict = live_verdict(evaluation.report.approved, evaluation.ineligible_reason)
    pt = {
        "APROVADO": "Critérios cumpridos (o Live ainda não está disponível nesta versão)",
        "NAO aprovado": "Bloqueado: critérios ainda não cumpridos",
    }.get(
        verdict,
        "Sem teste: não há diário para avaliar"
        if evaluation.no_test
        else "Inelegível: calendário de eventos aberto",
    )
    return LiveGateOut(
        run_id=evaluation.run_id,
        mode="paper",
        approved=evaluation.report.approved,
        eligible_for_live=evaluation.eligible,
        verdict=pt,
        ineligible_reason=evaluation.reason_text,
        criteria=criteria_of(evaluation),
        phrase_required=LIVE_PHRASE,
        live_available=LIVE_AVAILABLE,
        unavailable_reason=LIVE_UNAVAILABLE,
        first_activation_reduced_risk_days=FIRST_ACTIVATION_REDUCED_DAYS,
    )


class LiveGateService:
    def __init__(self, container: Container) -> None:
        self._c = container

    async def evaluate(self, run_id: str | None, *, fresh: bool = False) -> GateEvaluation:
        key = f"gate:{run_id}"
        cache = self._c.gate_cache
        cached = cache.get(key)
        if cached is not None and not fresh and time.monotonic() - cached[0] < GATE_TTL_SECONDS:
            cache[key] = cache.pop(key)
            value: GateEvaluation = cached[1]
            return value
        evaluation = await self._compute(run_id)
        cache.pop(key, None)
        if not evaluation.no_test:
            # só run que existe entra no cache (run_id inventado não pode crescer a memória) e é LRU limitado
            cache[key] = (time.monotonic(), evaluation)
            while len(cache) > GATE_CACHE_SIZE:
                cache.pop(next(iter(cache)))
        return evaluation

    async def _compute(self, run_id: str | None) -> GateEvaluation:
        now = self._c.clock()
        inputs: ReportInputs | None = None
        if run_id is not None:
            try:
                inputs = await collect_inputs(self._c.sessions, run_id, now=now, compare=False)
            except UnknownRunError:
                log.info("live_gate_without_journal", run_id=run_id)
        risk = inputs.risk if inputs is not None else RiskConfig()
        capital = inputs.capital if inputs is not None else Decimal(10_000)
        metrics = compute_paper_metrics(
            initial_equity=capital,
            trades=inputs.trades if inputs else [],
            curve=inputs.curve if inputs else [],
            events=inputs.events if inputs else [],
            now=now,
        )
        reason = inputs.ineligible_reason if inputs is not None else NO_TEST_CODE
        ineligible = (
            NO_TEST_CODE if inputs is None else (None if reason is None else INELIGIBLE_OPEN_CALENDAR)
        )
        thresholds = LiveGateThresholds.from_risk(risk)
        report = evaluate_live_gate(metrics, thresholds, ineligible_reason=reason)
        return GateEvaluation(
            run_id=run_id,
            report=report,
            thresholds=thresholds,
            ineligible_reason=ineligible,
            inputs=inputs,
        )
