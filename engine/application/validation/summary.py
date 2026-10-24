from collections import Counter
from collections.abc import Sequence
from datetime import datetime

from engine.application.backtest.report import ReportMeta, _meta_lines, _num, _pct, _table
from engine.application.records import Record
from engine.application.strategies.registry import is_applicable
from engine.application.validation.models import EntryReport, ValidationReport, Verdict
from engine.domain.types import Timeframe

SELECTOR_NAME = "seletor"


class Comparison(Record):
    symbol: str
    timeframe: str
    name: str


class CellInfo(Record):
    """Proveniencia de uma celula (par x timeframe)."""

    symbol: str
    timeframe: str
    candles: int
    period_start: datetime
    period_end: datetime
    source: str
    notes: tuple[str, ...] = ()


class Rollup(Record):
    """Uma estrategia (ou o seletor) em todas as celulas. Veredito: SUPERA se alguma celula passa o alfa
    corrigido pela familia inteira; PIOR se toda celula conclusiva e pior; amostra insuficiente se nenhuma
    celula tem trades suficientes; censurado se as demais celulas foram censuradas; senao, nao
    distinguivel."""

    name: str
    verdict: Verdict
    counts: dict[str, int]
    trades: int
    mean_r: float | None
    worst_drawdown_r: float
    passing: tuple[str, ...]
    max_idle_fraction: float | None = None


class ValidationSummary(Record):
    end: datetime
    comparisons: tuple[Comparison, ...]
    alpha: float
    alpha_corrected: float
    correction: str
    simulations: int
    seed: int
    min_attainable_p: float
    underpowered: bool
    cells: tuple[CellInfo, ...]
    reports: tuple[ValidationReport, ...]
    rollups: tuple[Rollup, ...]
    any_passed: bool
    conclusion: str


def _items(report: ValidationReport) -> list[EntryReport]:
    return [*report.strategies, report.selector]


def headline(verdicts: Sequence[Verdict]) -> Verdict:
    conclusive = [v for v in verdicts if v not in (Verdict.AMOSTRA_INSUFICIENTE, Verdict.CENSURADO)]
    if not conclusive:
        return Verdict.CENSURADO if Verdict.CENSURADO in verdicts else Verdict.AMOSTRA_INSUFICIENTE
    if Verdict.SUPERA in conclusive:
        return Verdict.SUPERA
    if all(v is Verdict.PIOR for v in conclusive):
        return Verdict.PIOR
    return Verdict.NAO_DISTINGUIVEL


def build_rollups(reports: Sequence[ValidationReport]) -> list[Rollup]:
    names = [e.name for e in _items(reports[0])]
    out: list[Rollup] = []
    for name in names:
        rows = [(r, e) for r in reports for e in _items(r) if e.name == name]
        verdicts = [e.judgement.verdict for _, e in rows]
        trades = sum(e.stats.trades for _, e in rows)
        total_r = sum(e.stats.total_r for _, e in rows)
        out.append(
            Rollup(
                name=name,
                verdict=headline(verdicts),
                counts=dict(Counter(v.value for v in verdicts)),
                trades=trades,
                mean_r=total_r / trades if trades else None,
                worst_drawdown_r=max(e.stats.max_drawdown_r for _, e in rows),
                max_idle_fraction=max(
                    (e.idle_fraction for _, e in rows if e.idle_fraction is not None), default=None
                ),
                passing=tuple(
                    f"{r.symbol} {r.timeframe}" for r, e in rows if e.judgement.verdict is Verdict.SUPERA
                ),
            )
        )
    return out


def conclude(rollups: Sequence[Rollup], *, underpowered: bool = False) -> tuple[bool, str]:
    passed = [r for r in rollups if r.verdict is Verdict.SUPERA]
    if underpowered:
        return False, (
            "Teste sem poder: o menor p-valor atingível com as simulações usadas não alcança o alfa "
            "corrigido, "
            "então nenhum veredito 'supera' é possível; refaça com mais simulações. O robô deve ficar parado."
        )
    if not passed:
        return False, (
            "Nenhuma estratégia nem o seletor superou as referências fora da amostra depois da correção de "
            "múltiplas comparações: o robô deve ficar parado e nenhum score deve ser publicado."
        )
    names = ", ".join(f"{r.name} ({'; '.join(r.passing)})" for r in passed)
    return True, f"Passaram nas referências fora da amostra (com correção de múltiplas comparações): {names}."


def build_summary(
    reports: Sequence[ValidationReport], cells: Sequence[CellInfo], end: datetime
) -> ValidationSummary:
    first = reports[0]
    rollups = build_rollups(reports)
    comparisons = tuple(
        Comparison(symbol=r.symbol, timeframe=r.timeframe, name=e.name)
        for r in reports
        for e in _items(r)
        if e.name == SELECTOR_NAME or is_applicable(e.name, Timeframe(r.timeframe))
    )
    judgement = first.selector.judgement
    passed, text = conclude(rollups, underpowered=judgement.underpowered)
    return ValidationSummary(
        end=end,
        comparisons=comparisons,
        alpha=judgement.alpha,
        alpha_corrected=judgement.alpha_corrected,
        correction=judgement.correction,
        simulations=first.config.simulations,
        seed=first.config.seed,
        min_attainable_p=judgement.min_attainable_p,
        underpowered=judgement.underpowered,
        cells=tuple(cells),
        reports=tuple(reports),
        rollups=tuple(rollups),
        any_passed=passed,
        conclusion=text,
    )


def render_summary_markdown(summary: ValidationSummary, meta: ReportMeta) -> str:
    lines = [
        f"# Validação do seletor e das estratégias ({meta.name})",
        "",
        *_meta_lines(meta),
        f"- Fim dos dados (fixo): {summary.end:%Y-%m-%d %H:%M} UTC; simulações {summary.simulations} "
        f"(semente {summary.seed})",
        f"- Comparações testadas: {len(summary.comparisons)}; correção {summary.correction}, alfa "
        f"{summary.alpha} => alfa por comparação {summary.alpha_corrected:.5f}",
        f"- Poder: menor p atingível {summary.min_attainable_p:.6f} com {summary.simulations} simulações; "
        + (
            "TESTE SEM PODER (menor p >= alfa corrigido): nenhum veredito 'supera' é emitido"
            if summary.underpowered
            else "suficiente (menor p < alfa corrigido)"
        ),
        "",
        "## Conclusão",
        "",
        summary.conclusion,
        "",
        "## Veredito por item (todas as células)",
        "",
        _table(
            [
                "Item",
                "Veredito",
                "Células (veredito: n)",
                "Trades OOS",
                "Expectativa (R)",
                "Pior drawdown (R)",
                "Maior fração parada",
            ],
            [
                [
                    r.name,
                    r.verdict.value,
                    ", ".join(f"{k}: {v}" for k, v in r.counts.items()),
                    r.trades,
                    _num(r.mean_r, 3),
                    _num(r.worst_drawdown_r),
                    _pct(r.max_idle_fraction, 1),
                ]
                for r in summary.rollups
            ],
        ),
        "",
        "## Detalhe por par e timeframe",
        "",
        _table(
            [
                "Par",
                "TF",
                "Item",
                "Trades",
                "Exp. (R)",
                "R total",
                "DD (R)",
                "Retorno",
                "DD",
                "Parada",
                "Exposição",
                "Ret./exposição",
                "Ret./DD",
                "p melhor",
                "p pior",
                "Veredito",
            ],
            [
                [
                    r.symbol,
                    r.timeframe,
                    e.name,
                    e.stats.trades,
                    _num(e.stats.mean_r, 3),
                    _num(e.stats.total_r),
                    _num(e.stats.max_drawdown_r),
                    _pct(e.stats.total_return),
                    _pct(e.stats.max_drawdown),
                    _pct(e.idle_fraction, 1),
                    _pct(e.exposure, 1),
                    _pct(e.return_per_exposure),
                    _num(e.return_over_drawdown),
                    _num(e.judgement.p_better, 4),
                    _num(e.judgement.p_worse, 4),
                    e.judgement.verdict.value,
                ]
                for r in summary.reports
                for e in _items(r)
            ],
        ),
        "",
        "## Comprar e segurar (mesmas janelas de teste, 100% exposto)",
        "",
        "Critério que DECIDE o veredito contra o comprar-e-segurar: retorno bruto >= B&H, OU "
        "retorno/drawdown "
        ">= o do B&H (ambos positivos). Retorno por unidade de exposição (coluna da tabela de detalhe, "
        "retorno / fração do tempo em posição) é apenas informativo: a estratégia fica pouco exposta e o "
        "B&H, 100%.",
        "",
        _table(
            ["Par", "TF", "Retorno", "Drawdown", "Ret./DD"],
            [
                [
                    r.symbol,
                    r.timeframe,
                    _pct(r.buy_hold.total_return),
                    _pct(r.buy_hold.max_drawdown),
                    _num(r.buy_hold.return_over_drawdown),
                ]
                for r in summary.reports
            ],
        ),
        "",
        "## Censura por travas de risco",
        "",
        "A coleta roda SEM o halt permanente por drawdown (mantém stop, R:R, risco por operação, limites de "
        "posição, perda diária, meta diária e pausa por perdas seguidas). 'Parada' = fração do período fora "
        "da amostra com alguma trava ativa. Acima de 30% o veredito vira 'censurado'. O seletor não tem "
        "fração parada (troca de estratégia por janela).",
        "",
        "## Limitações",
        "",
        "- A referência aleatória usa stop e alvo fixos (horizonte de 72 velas) SEM saída parcial, breakeven "
        "nem trailing: é uma régua aproximada, não a mesma gestão da estratégia real.",
        "- VWAP não opera em 4h (sessão UTC de 6 velas < mínimo de 8): a célula vwap x 4h foi excluída da "
        "contagem de comparações (m).",
        "",
        "## Dados",
        "",
        _table(
            ["Par", "TF", "Velas", "Período", "Fonte"],
            [
                [
                    c.symbol,
                    c.timeframe,
                    c.candles,
                    f"{c.period_start:%Y-%m-%d} a {c.period_end:%Y-%m-%d}",
                    c.source,
                ]
                for c in summary.cells
            ],
        ),
        "",
        "## Lista das comparações testadas",
        "",
        *[f"{i}. {c.symbol} {c.timeframe}: {c.name}" for i, c in enumerate(summary.comparisons, start=1)],
        "",
    ]
    notes = [f"- {c.symbol} {c.timeframe}: {n}" for c in summary.cells for n in c.notes]
    if notes:
        lines += ["## Notas dos dados", "", *notes, ""]
    return "\n".join(lines)
