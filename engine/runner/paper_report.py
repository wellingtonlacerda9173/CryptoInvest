"""Relatorio do paper (diario e acumulado): metricas, criterios do Live, comparacao com o backtest."""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic_core import to_jsonable_python

from engine.application.backtest.models import EquityPoint
from engine.application.paper_metrics import (
    LiveGateReport,
    LiveGateThresholds,
    PaperBacktestComparison,
    PaperMetrics,
    PeriodPerformance,
    active_intervals,
    compare_paper_backtest,
    compute_paper_metrics,
    evaluate_live_gate,
)
from engine.domain.journal import ControlEvent
from engine.domain.risk import RiskConfig
from engine.domain.trade import Trade
from engine.domain.types import Symbol, Timeframe

_DAY = timedelta(days=1)
INELIGIBLE_OPEN_CALENDAR = "calendario aberto"


@dataclass(frozen=True)
class DecisionSummary:
    decided_at: datetime
    symbol: str
    code: str
    traded: bool


@dataclass(frozen=True)
class BacktestSide:
    """Resultado do backtest nos mesmos candles/periodo; `note` explica quando nao foi possivel comparar."""

    symbol: Symbol
    trades: tuple[Trade, ...] = ()
    candles: int = 0
    note: str | None = None


@dataclass
class ReportInputs:
    run_id: str
    capital: Decimal
    risk: RiskConfig
    timeframe: Timeframe
    symbols: tuple[Symbol, ...]
    strategies: tuple[str, ...]
    trades: Sequence[Trade]
    curve: Sequence[EquityPoint]
    events: Sequence[ControlEvent]
    decisions: Sequence[DecisionSummary]
    now: datetime
    allow_open_calendar: bool = False
    code_version: str | None = None
    backtests: list[BacktestSide] = field(default_factory=list)

    @property
    def ineligible_reason(self) -> str | None:
        """`allow_open_calendar` vale para o run INTEIRO: basta um start ou uma decisao sem calendario."""
        return INELIGIBLE_OPEN_CALENDAR if self.allow_open_calendar else None


def _dec(value: Decimal) -> str:
    """Decimal legivel (sem notacao cientifica nem zeros a direita)."""
    return format(value.normalize(), "f") if value else "0"


def _metrics_block(metrics: PaperMetrics) -> dict[str, Any]:
    return {k: _dec(v) if isinstance(v, Decimal) else v for k, v in metrics.model_dump().items()}


def _decision_counts(decisions: Sequence[DecisionSummary]) -> dict[str, int]:
    return dict(sorted(Counter(d.code for d in decisions).items()))


def _period(inputs: ReportInputs) -> tuple[datetime | None, datetime]:
    intervals = active_intervals(inputs.events, inputs.now)
    return (intervals[0][0] if intervals else None), inputs.now


def _honesty(
    inputs: ReportInputs, trades: Sequence[Trade], decisions: Sequence[DecisionSummary]
) -> list[str]:
    notes: list[str] = []
    if not trades:
        entered = sum(d.traded for d in decisions)
        notes.append(
            "Nenhuma operacao foi encerrada no periodo. Ficar parado em USDT e um resultado valido: o "
            f"robo so opera se estrategia, filtros e risco concordam ({len(decisions)} decisoes avaliadas, "
            f"{entered} aprovadas)."
        )
    if inputs.allow_open_calendar:
        notes.append(
            "ATENCAO: o run usou --allow-open-calendar (calendario sem cobertura). Este resultado NAO "
            "vale como evidencia para liberar o Live."
        )
    return notes


def build_daily(inputs: ReportInputs, day: date) -> dict[str, Any]:
    start = datetime.combine(day, time(), tzinfo=UTC)
    end = min(start + _DAY, inputs.now)
    before = [t for t in inputs.trades if t.closed_at < start]
    initial = inputs.capital + sum((t.pnl for t in before), Decimal(0))
    trades = [t for t in inputs.trades if start <= t.closed_at < start + _DAY]
    curve = [p for p in inputs.curve if start <= p.at < start + _DAY]
    decisions = [d for d in inputs.decisions if start <= d.decided_at < start + _DAY]
    metrics = compute_paper_metrics(
        initial_equity=initial, trades=trades, curve=curve, events=inputs.events, now=max(end, start)
    )
    return {
        "scope": "daily",
        "run_id": inputs.run_id,
        "day": day.isoformat(),
        "generated_at": inputs.now.isoformat(),
        "complete_day": start + _DAY <= inputs.now,
        "initial_equity": _dec(initial),
        "final_equity": _dec(curve[-1].equity) if curve else None,
        "trades": [to_jsonable_python(t.model_dump()) | {"pnl": str(t.pnl)} for t in trades],
        "decisions": {"total": len(decisions), "by_code": _decision_counts(decisions)},
        "metrics": _metrics_block(metrics),
        "equity_points": len(curve),
        "notes": _honesty(inputs, trades, decisions),
    }


def build_cumulative(inputs: ReportInputs) -> dict[str, Any]:
    metrics = compute_paper_metrics(
        initial_equity=inputs.capital,
        trades=inputs.trades,
        curve=inputs.curve,
        events=inputs.events,
        now=inputs.now,
    )
    gate: LiveGateReport = evaluate_live_gate(
        metrics, LiveGateThresholds.from_risk(inputs.risk), ineligible_reason=inputs.ineligible_reason
    )
    start, end = _period(inputs)
    comparisons: list[dict[str, Any]] = []
    for side in inputs.backtests:
        entry: dict[str, Any] = {"symbol": side.symbol, "candles": side.candles, "note": side.note}
        if side.note is None and start is not None and end > start:
            paper = PeriodPerformance(
                initial_equity=inputs.capital,
                trades=tuple(t for t in inputs.trades if t.symbol == side.symbol),
            )
            backtest = PeriodPerformance(initial_equity=inputs.capital, trades=side.trades)
            comparison: PaperBacktestComparison = compare_paper_backtest(
                paper, backtest, start=start, end=end
            )
            entry["comparison"] = to_jsonable_python(comparison.model_dump())
        comparisons.append(entry)
    final = inputs.curve[-1].equity if inputs.curve else inputs.capital
    return {
        "scope": "cumulative",
        "run_id": inputs.run_id,
        "generated_at": inputs.now.isoformat(),
        "period": {"start": start.isoformat() if start else None, "end": end.isoformat()},
        "config": {
            "symbols": inputs.symbols,
            "timeframe": inputs.timeframe.value,
            "strategies": inputs.strategies,
            "capital": _dec(inputs.capital),
            "allow_open_calendar": inputs.allow_open_calendar,
            "code_version": inputs.code_version,
        },
        "initial_equity": _dec(inputs.capital),
        "final_equity": _dec(final),
        "return": _dec((final - inputs.capital) / inputs.capital) if inputs.capital > 0 else None,
        "decisions": {"total": len(inputs.decisions), "by_code": _decision_counts(inputs.decisions)},
        "metrics": _metrics_block(metrics),
        "live_gate": {
            "approved": gate.approved,
            "eligible_for_live": inputs.ineligible_reason is None,
            "ineligible_reason": inputs.ineligible_reason,
            "verdict": live_verdict(gate.approved, inputs.ineligible_reason),
            "criteria": [to_jsonable_python(c.model_dump()) for c in gate.criteria],
        },
        "backtest_comparison": comparisons,
        "notes": _honesty(inputs, inputs.trades, inputs.decisions),
    }


def live_verdict(approved: bool, ineligible_reason: str | None) -> str:
    """Texto unico para CLI e Markdown: run inelegivel nunca aparece como aprovado."""
    if ineligible_reason is not None:
        return f"INELEGIVEL: {ineligible_reason}"
    return "APROVADO" if approved else "NAO aprovado"


def to_markdown(report: dict[str, Any]) -> str:
    daily = report["scope"] == "daily"
    title = f"Paper {report['run_id']} - " + (f"dia {report['day']}" if daily else "acumulado")
    lines = [f"# {title}", "", f"Gerado em {report['generated_at']} (UTC)", ""]
    for note in report["notes"]:
        lines += [f"> {note}", ""]
    metrics = report["metrics"]
    lines += ["## Metricas", "", "| Metrica | Valor |", "|---|---|"]
    lines += [f"| {name} | {value} |" for name, value in metrics.items()]
    lines += ["", "## Decisoes", "", f"Total avaliado: {report['decisions']['total']}", ""]
    lines += [f"- `{code}`: {count}" for code, count in report["decisions"]["by_code"].items()] or [
        "- nenhuma"
    ]
    if daily:
        lines += ["", "## Operacoes do dia", ""]
        trades = report["trades"]
        lines += [
            f"- {t['symbol']} {t['exit_reason']} pnl {t['pnl']} ({t['opened_at']} -> {t['closed_at']})"
            for t in trades
        ] or ["- nenhuma"]
        lines += ["", f"Capital inicial do dia {report['initial_equity']}; final {report['final_equity']}."]
    else:
        gate = report["live_gate"]
        lines += ["", f"## Criterios para o Live ({gate['verdict']})", ""]
        lines += ["| Criterio | Exigido | Atual | Atendido |", "|---|---|---|---|"]
        lines += [
            f"| {c['description']} | {c['required']} | {c['actual']} | {'sim' if c['met'] else 'nao'}"
            f"{' (' + c['reason'] + ')' if c['reason'] else ''} |"
            for c in gate["criteria"]
        ]
        lines += [
            "",
            f"Capital {report['initial_equity']} -> {report['final_equity']} (retorno {report['return']}).",
        ]
        lines += ["", "## Paper x backtest (mesmos candles e periodo)", ""]
        for item in report["backtest_comparison"]:
            if "comparison" in item:
                c = item["comparison"]
                lines.append(
                    f"- {item['symbol']}: paper {c['paper_trades']} trades / pnl {c['paper_pnl']} | backtest "
                    f"{c['backtest_trades']} trades / pnl {c['backtest_pnl']} | pareados {c['matched']}"
                )
            else:
                lines.append(f"- {item['symbol']}: nao comparado ({item['note'] or 'periodo sem dados'})")
        lines.append("")
        lines.append(
            "O capital do paper e compartilhado entre os pares; cada backtest roda um par com o capital todo."
        )
    return "\n".join(lines) + "\n"


def write_report(directory: Path, name: str, report: dict[str, Any]) -> tuple[Path, Path]:
    import json

    directory.mkdir(parents=True, exist_ok=True)
    json_path, md_path = directory / f"{name}.json", directory / f"{name}.md"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    md_path.write_text(to_markdown(report), encoding="utf-8")
    return json_path, md_path
