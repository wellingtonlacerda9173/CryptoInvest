"""Medicao honesta do valor da IA: o que cada veto teria rendido (simulacao) contra o que foi executado.

Ganho/perda "evitados" vem de simulacao pessimista (mesmas regras do backtest) e vale so como estimativa;
com amostra pequena o veredito e sempre 'amostra insuficiente' ou 'sem conclusao estatistica'.
"""

import math
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Literal

from engine.application.backtest.hypothetical import simulate_signal
from engine.application.backtest.models import HypotheticalOutcome, HypotheticalStatus
from engine.application.backtest.settings import BacktestSettings, default_rules
from engine.application.records import Record
from engine.domain.candle import Candle
from engine.domain.signal import Signal

MIN_RESOLVED = 10
CONCLUSIVE_RESOLVED = 30
Z = 1.645
MAX_CASES = 50
RESOLVED = (HypotheticalStatus.STOP, HypotheticalStatus.TARGET, HypotheticalStatus.TIMEOUT)

Verdict = Literal[
    "no_activity",
    "insufficient_sample",
    "no_conclusion",
    "ai_avoided_losses",
    "ai_missed_gains",
    "no_difference",
]


class VetoInput(Record):
    decided_at: datetime
    code: str
    strategy: str
    symbol: str
    signal: Signal
    equity: Decimal | None
    candles: tuple[Candle, ...]


class VetoCase(Record):
    decided_at: datetime
    code: str
    strategy: str
    symbol: str
    status: str
    pnl: float
    r_multiple: float


class ApprovedTrade(Record):
    strategy: str
    symbol: str
    pnl: float
    r_multiple: float


class AiValueReport(Record):
    period_start: datetime
    period_end: datetime
    calls: int
    failed_calls: int
    cost_usd: float
    vetoes: int
    simulated: int
    not_filled: int
    unresolved: int
    not_simulated: int
    avoided_loss: float
    missed_gain: float
    net_avoided: float
    correct_vetoes: int
    false_vetoes: int
    false_veto_rate: float | None
    mean_r_vetoed: float | None
    approved_trades: int
    approved_wins: int
    approved_losses: int
    approved_pnl: float
    approved_loss_pnl: float
    approved_total_r: float
    net_value_usd: float
    verdict: Verdict
    verdict_text: str
    cases: tuple[VetoCase, ...]


def simulate_vetoes(
    vetoes: Sequence[VetoInput], settings: BacktestSettings
) -> list[tuple[VetoInput, HypotheticalOutcome]]:
    """Reaproveita `simulate_signal` com o dimensionamento real (capital do instante do sinal)."""
    out: list[tuple[VetoInput, HypotheticalOutcome]] = []
    for veto in vetoes:
        if veto.equity is None or not veto.candles:
            outcome = HypotheticalOutcome(
                status=HypotheticalStatus.NOT_SIMULATED, pnl=Decimal(0), r_multiple=Decimal(0), candles=0
            )
        else:
            outcome = simulate_signal(
                veto.signal,
                veto.candles,
                0,
                equity=veto.equity,
                settings=settings,
                rules=default_rules(veto.symbol),
            )
        out.append((veto, outcome))
    return out


def _verdict(resolved: Sequence[float], net_avoided: float, has_activity: bool) -> tuple[Verdict, str]:
    count = len(resolved)
    if not has_activity:
        return "no_activity", "Sem atividade da IA no período: nada a medir."
    if count < MIN_RESOLVED:
        return (
            "insufficient_sample",
            f"Amostra insuficiente: só {count} vetos simulados (mínimo {MIN_RESOLVED}). "
            "Não dá para dizer se a IA ajuda ou atrapalha.",
        )
    mean = sum(resolved) / count
    se = math.sqrt(sum((r - mean) ** 2 for r in resolved) / (count - 1) / count)
    if count < CONCLUSIVE_RESOLVED:
        lean = "evitou perdas" if net_avoided > 0 else "deixou de ganhar"
        return (
            "no_conclusion",
            f"Sem conclusão estatística: {count} vetos simulados (precisa de {CONCLUSIVE_RESOLVED}). "
            f"Até agora a IA {lean}, mas isso pode ser acaso.",
        )
    if mean + Z * se < 0:
        return "ai_avoided_losses", "Os sinais vetados tinham resultado médio negativo: a IA evitou perdas."
    if mean - Z * se > 0:
        return "ai_missed_gains", "Os sinais vetados tinham resultado médio positivo: a IA barrou ganhos."
    return "no_difference", "Sem diferença estatisticamente distinguível entre o que a IA vetou e o resto."


def build_report(
    cases: Sequence[tuple[VetoInput, HypotheticalOutcome]],
    approved: Sequence[ApprovedTrade],
    *,
    calls: int,
    failed_calls: int,
    cost_usd: float,
    start: datetime,
    end: datetime,
) -> AiValueReport:
    simulated = [(v, o) for v, o in cases if o.status in RESOLVED]
    pnls = [float(o.pnl) for _, o in simulated]
    rs = [float(o.r_multiple) for _, o in simulated]
    avoided = round(-sum(p for p in pnls if p < 0), 2)
    missed = round(sum(p for p in pnls if p > 0), 2)
    net = round(avoided - missed, 2)
    wrong = sum(1 for p in pnls if p > 0)
    status = [o.status for _, o in cases]
    active = bool(calls or cases or approved)
    verdict, text = _verdict(rs, net, active)
    latest = sorted(simulated, key=lambda item: item[0].decided_at, reverse=True)[:MAX_CASES]
    return AiValueReport(
        period_start=start,
        period_end=end,
        calls=calls,
        failed_calls=failed_calls,
        cost_usd=round(cost_usd, 6),
        vetoes=len(cases),
        simulated=len(simulated),
        not_filled=status.count(HypotheticalStatus.NOT_FILLED),
        unresolved=status.count(HypotheticalStatus.UNRESOLVED),
        not_simulated=status.count(HypotheticalStatus.NOT_SIMULATED),
        avoided_loss=avoided,
        missed_gain=missed,
        net_avoided=net,
        correct_vetoes=sum(1 for p in pnls if p < 0),
        false_vetoes=wrong,
        false_veto_rate=round(wrong / len(pnls), 4) if pnls else None,
        mean_r_vetoed=round(sum(rs) / len(rs), 4) if rs else None,
        approved_trades=len(approved),
        approved_wins=sum(1 for t in approved if t.pnl > 0),
        approved_losses=sum(1 for t in approved if t.pnl < 0),
        approved_pnl=round(sum(t.pnl for t in approved), 2),
        approved_loss_pnl=round(sum(t.pnl for t in approved if t.pnl < 0), 2),
        approved_total_r=round(sum(t.r_multiple for t in approved), 4),
        net_value_usd=round(net - cost_usd, 2),
        verdict=verdict,
        verdict_text=text,
        cases=tuple(
            VetoCase(
                decided_at=v.decided_at,
                code=v.code,
                strategy=v.strategy,
                symbol=v.symbol,
                status=o.status.value,
                pnl=round(float(o.pnl), 2),
                r_multiple=round(float(o.r_multiple), 4),
            )
            for v, o in latest
        ),
    )
