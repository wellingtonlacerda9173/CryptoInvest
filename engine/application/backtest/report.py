import csv
import json
from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import Field

from engine.application.backtest.metrics import (
    SHARPE_NOTE,
    AvoidedSummary,
    Metrics,
    compute_active_metrics,
    halt_time,
    summarize_avoided,
)
from engine.application.backtest.models import BacktestResult, Record
from engine.application.backtest.walk_forward import WalkForwardResult, combo_label
from engine.domain.risk import MAX_DRAWDOWN


class ReportMeta(Record):
    """Proveniencia do relatorio: versao do codigo, dados e parametros usados."""

    name: str
    git_sha: str | None = None
    git_dirty: bool | None = None
    data_source: str = "n/d"
    data_notes: tuple[str, ...] = ()
    parameters: dict[str, Any] = Field(default_factory=dict)


def _pct(value: float | None, digits: int = 2) -> str:
    return "n/d" if value is None else f"{value * 100:.{digits}f}%"


def _num(value: float | None, digits: int = 2) -> str:
    return "n/d" if value is None else f"{value:.{digits}f}"


def _money(value: Decimal) -> str:
    return f"{value:,.2f}"


def _table(headers: Sequence[str], rows: Iterable[Sequence[object]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows]
    return "\n".join(lines)


def _write_csv(path: Path, headers: Sequence[str], rows: Iterable[Sequence[object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        writer.writerows(rows)


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _meta_lines(meta: ReportMeta) -> list[str]:
    version = (
        "n/d"
        if meta.git_sha is None
        else f"{meta.git_sha}{' (alteracoes nao commitadas)' if meta.git_dirty else ''}"
    )
    lines = [f"- Versao do codigo (git): {version}", f"- Fonte dos dados: {meta.data_source}"]
    lines += [f"- Dados: {note}" for note in meta.data_notes]
    return lines


def _metrics_section(
    m: Metrics, halted_at: datetime | None = None, halt_rows: list[list[Any]] | None = None
) -> list[str]:
    halted = "nao" if halted_at is None else f"sim, em {halted_at:%Y-%m-%d %H:%M} UTC"
    halt_rows = halt_rows or [["Robo desligado por drawdown", halted]]
    return [
        "## Risco e consistencia (prioridade do projeto)",
        "",
        _table(
            ["Indicador", "Valor"],
            [
                ["Drawdown maximo (equity marcado a mercado)", _pct(m.max_drawdown)],
                ["Maior tempo abaixo do pico", f"{m.max_drawdown_days:.1f} dias"],
                ["Meses positivos", _pct(m.positive_months_ratio, 1)],
                ["Pior mes", _pct(m.worst_month_return)],
                *halt_rows,
            ],
        ),
        "",
        "## Resultado",
        "",
        _table(
            ["Indicador", "Valor"],
            [
                ["Retorno total", _pct(m.total_return)],
                ["Resultado (USDT)", _money(m.total_pnl)],
                ["Capital final (marcado a mercado)", _money(m.final_equity)],
                ["Trades (posicoes encerradas)", m.trades],
                ["Ganhos / perdas / zerados", f"{m.wins} / {m.losses} / {m.breakeven}"],
                ["Taxa de acerto", _pct(m.win_rate, 1)],
                ["Profit factor", _num(m.profit_factor)],
                ["Payoff (ganho medio / perda media)", _num(m.payoff)],
                ["Expectativa (R por trade)", _num(m.expectancy_r, 3)],
                ["Sharpe (anualizado)", _num(m.sharpe)],
                ["Tempo parado (sem posicao)", _pct(m.idle_pct, 1)],
                ["Taxas pagas (USDT)", _money(m.fees_paid)],
            ],
        ),
        "",
        f"_{SHARPE_NOTE}._",
    ]


def _active_section(active: Metrics | None, halted_at: datetime | None) -> list[str]:
    if active is None or halted_at is None:
        return []
    return [
        f"## Trecho ativo (antes do desligamento em {halted_at:%Y-%m-%d %H:%M} UTC)",
        "",
        "O robo permanece desligado ate reativacao manual: o periodo total inclui o tempo parado apos o "
        "halt; estas sao as metricas apenas do trecho em que operou.",
        "",
        _table(
            ["Indicador", "Trecho ativo"],
            [
                ["Retorno", _pct(active.total_return)],
                ["Drawdown maximo", _pct(active.max_drawdown)],
                ["Trades", active.trades],
                ["Taxa de acerto", _pct(active.win_rate, 1)],
                ["Profit factor", _num(active.profit_factor)],
                ["Expectativa (R por trade)", _num(active.expectancy_r, 3)],
                ["Sharpe (anualizado)", _num(active.sharpe)],
            ],
        ),
        "",
    ]


def _by_strategy(m: Metrics) -> list[str]:
    rows = [
        [g.name, g.trades, _pct(g.win_rate, 1), _money(g.pnl), _num(g.profit_factor), _num(g.expectancy_r, 3)]
        for g in m.by_strategy
    ]
    if not rows:
        return ["Nenhum trade encerrado.", ""]
    return [
        _table(["Estrategia", "Trades", "Acerto", "Resultado", "Profit factor", "Expectativa R"], rows),
        "",
    ]


def _monthly(m: Metrics) -> list[str]:
    rows = [[x.month, _pct(x.return_pct), _money(x.realized_pnl), x.trades] for x in m.monthly]
    return [_table(["Mes", "Retorno (equity)", "Resultado realizado", "Trades fechados"], rows), ""]


_KIND_LABEL = {
    "capacity": "capacidade",
    "state_lock": "trava de estado",
    "filter": "filtro de mercado/risco",
    "selector": "seletor de estrategias",
    "ai": "analista de IA",
}


def _avoided(summary: AvoidedSummary) -> list[str]:
    by_filter = sum(g.signals for g in summary.by_code if g.kind == "filter")
    filter_effect = (
        _money(summary.risk_vetoes_net_effect) + " USDT"
        if by_filter
        else "n/a (0 sinais barrados por filtro)"
    )
    lines = [
        f"Sinais gerados e nao operados: {summary.signals} (simulados ate alvo/stop/limite de tempo: "
        f"{summary.simulated}). Cada sinal e simulado de forma independente; os totais sao a SOMA de "
        "simulacoes independentes, nao o efeito sobre o capital (os sinais competiriam por posicao e o "
        f"capital mudaria). Perda evitada: {_money(summary.avoided_losses)} USDT; ganho perdido: "
        f"{_money(summary.forgone_gains)} USDT; soma liquida: {_money(summary.net_effect)} USDT "
        f"(positivo = poupou dinheiro). Acerto hipotetico dos sinais nao operados: "
        f"{_pct(summary.hypothetical_win_rate, 1)}.",
        "",
        _table(
            ["Grupo de vetos", "Soma liquida (USDT)", "Entra no efeito dos vetos de risco/filtros"],
            [
                ["Filtros de mercado/risco", _money(summary.risk_vetoes_net_effect), "sim"],
                [
                    "Travas de estado (halt, pausas, meta diaria)",
                    _money(summary.state_locks_net_effect),
                    "nao",
                ],
                ["Capacidade (ja ha posicao/ordem)", _money(summary.capacity_net_effect), "nao"],
            ],
        ),
        "",
        f"**Efeito liquido dos vetos de risco/filtros: {filter_effect}.**",
        "",
    ]
    if summary.halt_at is not None:
        lines += [
            f"Efeito do desligamento (halt em {summary.halt_at:%Y-%m-%d %H:%M} UTC): "
            f"{summary.halt_signals} sinais barrados ({summary.halt_simulated} simulados), soma liquida "
            f"{_money(summary.halt_net_effect)} USDT. Nao e um veto de risco: e o robo parado.",
            "",
        ]
    if summary.by_code:
        rows = [
            [
                g.code,
                _KIND_LABEL[g.kind],
                g.signals,
                g.simulated,
                _money(g.avoided_losses),
                _money(g.forgone_gains),
                _money(g.hypothetical_pnl),
            ]
            for g in summary.by_code
        ]
        lines += [
            _table(
                [
                    "Motivo",
                    "Tipo",
                    "Sinais",
                    "Simulados",
                    "Perda evitada",
                    "Ganho perdido",
                    "Resultado hipotetico",
                ],
                rows,
            ),
            "",
        ]
    return lines


def render_backtest_markdown(result: BacktestResult, metrics: Metrics, meta: ReportMeta) -> str:
    events = Counter(e.action.value for e in result.risk_events)
    halted_at = halt_time(result.risk_events)
    traded = Counter(d.code for d in result.decisions if d.action.value == "traded")
    skipped = Counter(d.code for d in result.decisions if d.action.value == "skipped")
    lines = [
        f"# Backtest {result.symbol} {result.timeframe} - {meta.name}",
        "",
        f"Periodo: {result.period_start:%Y-%m-%d %H:%M} a {result.period_end:%Y-%m-%d %H:%M} UTC "
        f"({result.candles} velas). Estrategias: {', '.join(result.strategies)}. "
        f"Capital inicial: {_money(result.initial_capital)} USDT.",
        "",
        *_metrics_section(metrics, halted_at),
        "",
        *_active_section(compute_active_metrics(result), halted_at),
        "## Por estrategia",
        "",
        *_by_strategy(metrics),
        "## Resultado por mes",
        "",
        *_monthly(metrics),
        "## Decisoes",
        "",
        f"- Velas sem sinal de nenhuma estrategia: {result.steps_without_signal} avaliacoes",
        f"- Ordens aprovadas: {sum(traded.values())} ({dict(traded)}); "
        f"nao operadas: {sum(skipped.values())} {dict(skipped)}",
        f"- Posicoes abertas no fim dos dados (nao contadas como trade): {result.open_at_end}",
        f"- Eventos de risco: {dict(events) or 'nenhum'}",
        "",
        "## Operacoes evitadas",
        "",
        *_avoided(summarize_avoided(result.decisions, result.risk_events)),
        "## Premissas",
        "",
        f"- Taxas: taker {result.settings.risk.taker_fee_rate}, maker {result.settings.maker_fee_rate}; "
        "slippage "
        f"{result.settings.slippage_bps} bps; latencia {result.settings.latency_candles} vela(s); "
        "custo de slippage "
        f"acumulado {_money(result.total_slippage_cost)} USDT",
        "- Entradas: ordem limite no fechamento do sinal, executada a partir da vela seguinte; validade de "
        f"{result.settings.risk.max_signal_age_timeframes} velas (cancelada antes de qualquer vela que abra "
        "apos a validade)",
        "- Fill por toque (otimista): o limite executa assim que a minima da vela o alcanca, sem fila no "
        "livro nem garantia de volume naquele preco; ao mesmo tempo o fill e ao preco limite, sem melhora "
        "de preco (pessimista); na mesma vela, stop antes do alvo; slippage sempre contra o trader",
        "- Sem spread nem livro de ofertas simulados; volume 24h calculado das proprias velas; "
        "sem calendario de eventos no backtest",
        "- Sem filtro de tendencia em timeframe superior (HTF)",
        "- Operacoes evitadas: simulacao hipotetica sem saida parcial nem trailing (sai no alvo, no stop ou "
        "no limite de tempo), tamanho pelo mesmo dimensionamento do gate (risco e teto de posicao), cada "
        "sinal de forma independente",
        "- Rotulos de saida: `trailing_stop`/`breakeven_stop` quando o stop foi movido a favor e a saida e "
        "lucrativa (rotulo so do relatorio; o motivo no dominio segue `stop_loss`)",
        "",
        "## Origem",
        "",
        *_meta_lines(meta),
        "",
    ]
    return "\n".join(lines)


def write_backtest_report(
    out_dir: Path, result: BacktestResult, metrics: Metrics, meta: ReportMeta
) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = summarize_avoided(result.decisions, result.risk_events)
    active = compute_active_metrics(result)
    payload = {
        "meta": meta.model_dump(mode="json"),
        "period": {
            "start": result.period_start.isoformat(),
            "end": result.period_end.isoformat(),
            "candles": result.candles,
        },
        "symbol": result.symbol,
        "timeframe": result.timeframe,
        "strategies": list(result.strategies),
        "settings": result.settings.model_dump(mode="json"),
        "metrics": metrics.model_dump(mode="json"),
        "avoided_operations": summary.model_dump(mode="json"),
        "halt": {
            "at": summary.halt_at.isoformat() if summary.halt_at else None,
            "active_metrics": active.model_dump(mode="json") if active else None,
        },
        "risk_events": [e.model_dump(mode="json") for e in result.risk_events],
        "steps_without_signal": result.steps_without_signal,
        "open_at_end": result.open_at_end,
        "halted": result.halted,
        "total_slippage_cost": str(result.total_slippage_cost),
    }
    files = {
        name: out_dir / name
        for name in (
            "report.json",
            "report.md",
            "trades.csv",
            "roundtrips.csv",
            "equity.csv",
            "decisions.csv",
        )
    }
    _write_json(files["report.json"], payload)
    files["report.md"].write_text(render_backtest_markdown(result, metrics, meta), encoding="utf-8")
    _write_csv(
        files["trades.csv"],
        [
            "trade_id",
            "symbol",
            "quantity",
            "entry_price",
            "exit_price",
            "entry_fee",
            "exit_fee",
            "opened_at",
            "closed_at",
            "exit_reason",
            "pnl",
        ],
        (
            [
                t.trade_id,
                t.symbol,
                t.quantity,
                t.entry_price,
                t.exit_price,
                t.entry_fee,
                t.exit_fee,
                t.opened_at.isoformat(),
                t.closed_at.isoformat(),
                t.exit_reason.value,
                t.pnl,
            ]
            for t in result.trades
        ),
    )
    _write_csv(files["roundtrips.csv"], *_roundtrip_table(result.round_trips))
    _write_csv(
        files["equity.csv"],
        ["at", "equity", "realized", "in_position"],
        ([p.at.isoformat(), p.equity, p.realized, int(p.in_position)] for p in result.equity_curve),
    )
    _write_csv(
        files["decisions.csv"],
        [
            "at",
            "strategy",
            "action",
            "code",
            "confidence",
            "entry",
            "stop",
            "target",
            "order_id",
            "hyp_status",
            "hyp_pnl",
            "hyp_r",
            "message",
        ],
        (
            [
                d.at.isoformat(),
                d.strategy,
                d.action.value,
                d.code,
                d.confidence,
                d.entry,
                d.stop,
                d.target,
                d.order_id,
                d.hypothetical.status.value if d.hypothetical else "",
                d.hypothetical.pnl if d.hypothetical else "",
                d.hypothetical.r_multiple if d.hypothetical else "",
                d.message,
            ]
            for d in result.decisions
        ),
    )
    return list(files.values())


def _roundtrip_table(trips: Sequence[Any]) -> tuple[list[str], list[list[object]]]:
    headers = [
        "order_id",
        "symbol",
        "strategy",
        "opened_at",
        "closed_at",
        "entry_price",
        "quantity",
        "pnl",
        "fees",
        "risk_amount",
        "r_multiple",
        "exit_reasons",
        "exit_labels",
    ]
    rows: list[list[object]] = [
        [
            t.order_id,
            t.symbol,
            t.strategy,
            t.opened_at.isoformat(),
            t.closed_at.isoformat(),
            t.entry_price,
            t.quantity,
            t.pnl,
            t.fees,
            t.risk_amount,
            t.r_multiple,
            "+".join(r.value for r in t.exit_reasons),
            "+".join(t.labels),
        ]
        for t in trips
    ]
    return headers, rows


def _walk_forward_halt_rows(result: WalkForwardResult) -> list[list[Any]]:
    run = [w for w in result.windows if w.test_result is not None]
    halted = sum(1 for w in run if w.test_result is not None and halt_time(w.test_result.risk_events))
    exceeds = result.out_of_sample.max_drawdown > float(MAX_DRAWDOWN)
    verdict = "sim" if exceeds else "nao"
    return [
        ["Robo desligado por drawdown", "n/d (cada janela reinicia o estado)"],
        ["Janelas desligadas por drawdown", f"{halted} de {len(run)}"],
        [f"Drawdown agregado excede o limite de {_pct(float(MAX_DRAWDOWN))}", verdict],
    ]


def render_walk_forward_markdown(
    result: WalkForwardResult, symbol: str, timeframe: str, meta: ReportMeta
) -> str:
    cfg, oos, assess = result.config, result.out_of_sample, result.assessment
    grid = ", ".join(f"{k} em {[str(v) for v in vs]}" for k, vs in sorted(result.grid.values.items()))
    positive = " e retorno positivo" if cfg.require_positive_train else ""
    rows = []
    for w in result.windows:
        train, test = w.train_metrics, w.test_metrics
        rows.append(
            [
                w.index,
                f"{w.train_start:%Y-%m-%d} a {w.train_end:%Y-%m-%d}",
                f"{w.test_start:%Y-%m-%d} a {w.test_end:%Y-%m-%d}",
                combo_label(w.selected) if w.selected else "parado (sem selecao)",
                "n/d"
                if train is None
                else f"{_pct(train.max_drawdown)} / {_pct(train.total_return)} / {train.trades}",
                "n/d"
                if test is None
                else f"{_pct(test.max_drawdown)} / {_pct(test.total_return)} / {test.trades}",
            ]
        )
    lines = [
        f"# Walk-forward {symbol} {timeframe} - {meta.name}",
        "",
        f"Treino {cfg.train_days} dias, teste {cfg.test_days} dias, avanco {cfg.step} dias; "
        f"{len(result.windows)} janelas. "
        f"Grade: {grid}. "
        "Criterio no treino: menor drawdown (faixas de 0,5 p.p.), depois mais meses positivos, "
        "depois maior retorno; "
        f"minimo de {cfg.min_train_trades} trades{positive}. "
        "Cada janela recomeca com o capital inicial e um RiskState novo (equivale a uma reativacao manual "
        "do robo a cada janela; no live o halt por drawdown e permanente); o agregado encadeia os retornos "
        "fora da amostra.",
        "",
        "## Fora da amostra (agregado)",
        "",
        *_metrics_section(oos, halt_rows=_walk_forward_halt_rows(result)),
        "",
        "## Dentro x fora da amostra (sobreajuste)",
        "",
        _table(
            ["Indicador", "Treino (dentro)", "Teste (fora)"],
            [
                [
                    "Retorno por 30 dias",
                    _pct(assess.in_sample_return_per_30d),
                    _pct(assess.out_of_sample_return_per_30d),
                ],
                [
                    "Expectativa (R)",
                    _num(assess.in_sample_expectancy_r, 3),
                    _num(assess.out_of_sample_expectancy_r, 3),
                ],
                [
                    "Drawdown maximo (media no treino)",
                    _pct(assess.in_sample_mean_drawdown),
                    _pct(assess.out_of_sample_drawdown),
                ],
                ["Selecoes distintas", assess.distinct_selections, "-"],
            ],
        ),
        "",
        f"**Veredito: {assess.verdict}.**",
        "",
        *[f"- Alerta: {flag}" for flag in assess.flags],
        "",
        "## Janelas (drawdown / retorno / trades)",
        "",
        _table(["#", "Treino", "Teste", "Parametros escolhidos", "Treino", "Teste"], rows),
        "",
        "## Origem",
        "",
        *_meta_lines(meta),
        "",
    ]
    return "\n".join(lines)


def write_walk_forward_report(
    out_dir: Path, result: WalkForwardResult, symbol: str, timeframe: str, meta: ReportMeta
) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "meta": meta.model_dump(mode="json"),
        "symbol": symbol,
        "timeframe": timeframe,
        "config": result.config.model_dump(mode="json"),
        "grid": result.grid.model_dump(mode="json"),
        "out_of_sample": result.out_of_sample.model_dump(mode="json"),
        "assessment": result.assessment.model_dump(mode="json"),
        "windows": [w.model_dump(mode="json", exclude={"test_result"}) for w in result.windows],
    }
    files = {
        name: out_dir / name
        for name in (
            "walk_forward.json",
            "walk_forward.md",
            "walk_forward_windows.csv",
            "oos_equity.csv",
            "oos_roundtrips.csv",
        )
    }
    _write_json(files["walk_forward.json"], payload)
    files["walk_forward.md"].write_text(
        render_walk_forward_markdown(result, symbol, timeframe, meta), encoding="utf-8"
    )
    _write_csv(
        files["walk_forward_windows.csv"],
        [
            "window",
            "train_start",
            "train_end",
            "test_start",
            "test_end",
            "selected",
            "train_trades",
            "train_max_dd",
            "train_return",
            "test_trades",
            "test_max_dd",
            "test_return",
        ],
        (
            [
                w.index,
                w.train_start.isoformat(),
                w.train_end.isoformat(),
                w.test_start.isoformat(),
                w.test_end.isoformat(),
                combo_label(w.selected) if w.selected else "",
                w.train_metrics.trades if w.train_metrics else "",
                w.train_metrics.max_drawdown if w.train_metrics else "",
                w.train_metrics.total_return if w.train_metrics else "",
                w.test_metrics.trades if w.test_metrics else "",
                w.test_metrics.max_drawdown if w.test_metrics else "",
                w.test_metrics.total_return if w.test_metrics else "",
            ]
            for w in result.windows
        ),
    )
    _write_csv(
        files["oos_equity.csv"],
        ["at", "equity", "in_position"],
        ([p.at.isoformat(), p.equity, int(p.in_position)] for p in result.out_of_sample_curve),
    )
    _write_csv(files["oos_roundtrips.csv"], *_roundtrip_table(result.out_of_sample_round_trips))
    return list(files.values())
