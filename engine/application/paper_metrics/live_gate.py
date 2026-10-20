from decimal import Decimal
from typing import Self

from engine.application.backtest.models import Record
from engine.application.paper_metrics.metrics import PaperMetrics
from engine.domain.risk import RiskConfig


class LiveGateThresholds(Record):
    """Criterios da trava do Live. O limite de perda diaria deve ser o mesmo do `RiskConfig` em uso."""

    max_daily_loss: Decimal
    min_days: int = 60
    max_drawdown: Decimal = Decimal("0.05")
    min_profit_factor: Decimal = Decimal("1.5")
    # amostra minima para o profit factor valer: poucos trades ou poucas perdas nao provam nada
    min_trades: int = 30
    min_losses: int = 5

    @classmethod
    def from_risk(cls, config: RiskConfig) -> Self:
        return cls(max_daily_loss=config.max_daily_loss)


class LiveGateCriterion(Record):
    code: str
    description: str
    required: str
    actual: str
    met: bool
    reason: str | None = None


class LiveGateReport(Record):
    criteria: tuple[LiveGateCriterion, ...]
    metrics: PaperMetrics

    @property
    def approved(self) -> bool:
        return all(c.met for c in self.criteria)

    @property
    def failed(self) -> tuple[LiveGateCriterion, ...]:
        return tuple(c for c in self.criteria if not c.met)


def evaluate_live_gate(
    metrics: PaperMetrics, thresholds: LiveGateThresholds, *, ineligible_reason: str | None = None
) -> LiveGateReport:
    """Todos os criterios precisam ser atendidos; dado ausente ou nao medido reprova.

    `ineligible_reason` (ex.: o run operou sem calendario de eventos, mesmo que so por um instante) acrescenta
    o criterio `eligible_for_live` reprovado: nenhum desempenho compensa evidencia gerada sem a protecao.

    Fronteiras: dias >= minimo; drawdown < limite (igual ao limite ja desligaria o robo) e medido; profit
    factor > minimo com amostra minima de trades e de perdas (sem perdas nao ha profit factor); perda diaria
    > limite reprova (igual ao limite so pausou o dia, como previsto).
    """
    drawdown = metrics.max_drawdown
    factor = metrics.profit_factor
    sample_gaps = [
        text
        for text, ok in (
            (
                f"{metrics.trades} trades (minimo {thresholds.min_trades})",
                metrics.trades >= thresholds.min_trades,
            ),
            (
                f"{metrics.losing_trades} perdas (minimo {thresholds.min_losses})",
                metrics.losing_trades >= thresholds.min_losses,
            ),
        )
        if not ok
    ]
    factor_met = not sample_gaps and factor is not None and factor > thresholds.min_profit_factor
    eligibility = (
        ()
        if ineligible_reason is None
        else (
            LiveGateCriterion(
                code="eligible_for_live",
                description="Run elegivel como evidencia para o Live",
                required="run inteiro com a protecao do calendario de eventos",
                actual="inelegivel",
                met=False,
                reason=ineligible_reason,
            ),
        )
    )
    return LiveGateReport(
        metrics=metrics,
        criteria=(
            LiveGateCriterion(
                code="operating_days",
                description="Dias de operacao acumulados",
                required=f">= {thresholds.min_days}",
                actual=str(metrics.operating_days),
                met=metrics.operating_days >= thresholds.min_days,
            ),
            LiveGateCriterion(
                code="max_drawdown",
                description="Drawdown maximo",
                required=f"< {thresholds.max_drawdown:.2%}",
                actual="nao medido" if drawdown is None else f"{drawdown:.2%}",
                met=drawdown is not None and drawdown < thresholds.max_drawdown,
                reason="curva de equity vazia ou sem cobertura do periodo" if drawdown is None else None,
            ),
            LiveGateCriterion(
                code="profit_factor",
                description="Profit factor",
                required=f"> {thresholds.min_profit_factor}, com {thresholds.min_trades} trades e "
                f"{thresholds.min_losses} perdas no minimo",
                actual=_fmt(factor),
                met=factor_met,
                reason=f"amostra insuficiente: {' e '.join(sample_gaps)}" if sample_gaps else None,
            ),
            LiveGateCriterion(
                code="daily_loss",
                description="Maior perda diaria",
                required=f"<= {thresholds.max_daily_loss:.2%}",
                actual=f"{metrics.worst_daily_loss_pct:.2%}",
                met=metrics.worst_daily_loss_pct <= thresholds.max_daily_loss,
            ),
            *eligibility,
        ),
    )


def _fmt(value: Decimal | None) -> str:
    return "n/d" if value is None else f"{value:.2f}"
