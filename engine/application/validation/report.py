from collections.abc import Sequence
from pathlib import Path

from engine.application.backtest.report import ReportMeta, _meta_lines, _num, _pct, _table, _write_json
from engine.application.validation.models import EntryReport, ValidationReport, WindowReport


def _entry_row(entry: EntryReport) -> list[object]:
    s, j = entry.stats, entry.judgement
    return [
        entry.name,
        s.trades,
        _num(s.mean_r, 3),
        _num(s.total_r),
        _pct(s.total_return),
        _pct(s.max_drawdown),
        _pct(entry.idle_fraction, 1),
        _num(j.p_better, 4),
        _num(j.p_worse, 4),
        j.verdict.value,
    ]


def _window_rows(windows: Sequence[WindowReport]) -> list[list[object]]:
    return [
        [
            w.index,
            w.test_start.date().isoformat(),
            w.test_end.date().isoformat(),
            "; ".join(f"{d.regime.value}: {d.chosen_label}" for d in w.decisions),
            w.selector_trades,
            _num(w.selector_total_r),
        ]
        for w in windows
    ]


def render_validation_markdown(report: ValidationReport, meta: ReportMeta) -> str:
    cfg = report.config
    lines = [
        f"# Validação fora da amostra: {report.symbol} {report.timeframe} ({meta.name})",
        "",
        *_meta_lines(meta),
        f"- Período avaliado: {report.period_start.date()} a {report.period_end.date()} "
        f"({len(report.windows)} janelas de {cfg.test_days} dias; treino mínimo {cfg.train_days_min} dias)",
        f"- Comparações testadas: {report.comparisons} (estratégias + seletor + {cfg.extra_comparisons}); "
        f"correção {cfg.correction}, alfa {cfg.alpha} => alfa por comparação "
        f"{report.selector.judgement.alpha_corrected:.4f}",
        f"- Amostra mínima: {report.min_trades} trades; referências: comprar e segurar e "
        f"{cfg.simulations} sorteios aleatórios (semente {cfg.seed}) com mesma contagem, stop e alvo",
        f"- Comprar e segurar no mesmo período: retorno {_pct(report.buy_hold.total_return)}, "
        f"drawdown {_pct(report.buy_hold.max_drawdown)}",
        "",
        "## Veredito",
        "",
        _table(
            [
                "Item",
                "Trades",
                "Expectativa (R)",
                "R total",
                "Retorno",
                "Drawdown",
                "Parada",
                "p melhor",
                "p pior",
                "Veredito",
            ],
            [_entry_row(e) for e in (*report.strategies, report.selector)],
        ),
        "",
        "p melhor/pior: fração dos sorteios aleatórios com expectativa igual ou melhor/pior que a real.",
    ]
    notes = [f"- {e.name}: {n}" for e in (*report.strategies, report.selector) for n in e.judgement.notes]
    if notes:
        lines += ["", "## Observações", "", *notes]
    lines += [
        "",
        "## Decisões do seletor por janela",
        "",
        _table(
            ["Janela", "Início", "Fim", "Escolha por regime", "Trades", "R"], _window_rows(report.windows)
        ),
        "",
    ]
    return "\n".join(lines)


def write_validation_report(out_dir: Path, report: ValidationReport, meta: ReportMeta) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path, md_path = out_dir / "validation.json", out_dir / "validation.md"
    _write_json(json_path, {"meta": meta.model_dump(mode="json"), "report": report.model_dump(mode="json")})
    md_path.write_text(render_validation_markdown(report, meta), encoding="utf-8")
    return [json_path, md_path]
