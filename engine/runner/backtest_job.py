"""Worker de backtest para a API: roda em SUBPROCESSO (nunca no event loop) e reaproveita o motor de sempre.

    python -m engine.runner.backtest_job --params <job.json> --out <resultado.json> [--max-memory-mb N]

Protocolo: o pai grava os parâmetros em arquivo (nada de dados do usuário em argv) e lê, linha a linha, o JSON
de progresso impresso aqui (`{"type": "progress", ...}`); o relatório final vai para o arquivo `--out`. O pai
impõe o tempo máximo (mata o processo) e o cancelamento; aqui um vigia limita a memória residual.
"""

import argparse
import asyncio
import json
import os
import sys
import threading
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from engine.application.backtest import (
    BacktestEngine,
    BacktestSettings,
    NotEnoughCandlesError,
    WalkForwardConfig,
    compute_metrics,
    default_rules,
    summarize_avoided,
    walk_forward,
)
from engine.application.backtest.models import BacktestResult, EquityPoint
from engine.domain.risk import RiskConfig
from engine.domain.types import Timeframe
from engine.runner.backtest import DEFAULT_GRID, build_strategies, parse_grid
from engine.runner.candle_source import load_candles

EXIT_MEMORY = 3
EXIT_FAILED = 1
MAX_CURVE_POINTS = 400
MAX_TRIPS = 500
VERDICTS = {
    "consistente": "Consistente: o resultado fora da amostra acompanha o do treino.",
    "inconclusivo (amostra pequena)": "Inconclusivo: poucas operações fora da amostra para concluir.",
    "sem edge detectavel fora da amostra": "Sem vantagem detectável fora da amostra.",
    "indicios de sobreajuste": "Indícios de sobreajuste: foi bem no treino e pior no teste.",
}


def frozen_clock(moment: datetime) -> Callable[[], datetime]:
    return lambda: moment


def emit(**message: Any) -> None:
    # ASCII puro: o pipe nao depende da codificacao do console (cp1252 no Windows) e o pai le sempre
    print(json.dumps(message, ensure_ascii=True, default=str), flush=True)


def rss_mb() -> float:
    """Memória residual deste processo em MB (Windows: psapi; Linux: /proc; demais: pico de uso)."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = Counters()
        counters.cb = ctypes.sizeof(Counters)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        if psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            return float(counters.WorkingSetSize) / 1_048_576
        return 0.0
    statm = Path("/proc/self/statm")
    if statm.exists():
        return int(statm.read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 1_048_576
    import resource

    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return usage / 1_048_576 if sys.platform == "darwin" else usage / 1024


def start_memory_guard(limit_mb: float, *, interval: float = 0.5, measure: Any = rss_mb) -> threading.Thread:
    """Vigia em segundo plano: estourou o limite, avisa o pai e encerra o processo na hora."""

    def watch() -> None:
        while True:
            time.sleep(interval)
            used = measure()
            if used > limit_mb:
                emit(type="error", code="memory_limit", message=f"limite de memória excedido ({used:.0f} MB)")
                os._exit(EXIT_MEMORY)

    thread = threading.Thread(target=watch, name="memory-guard", daemon=True)
    thread.start()
    return thread


def downsample[T](items: Sequence[T], limit: int) -> list[T]:
    if len(items) <= limit:
        return list(items)
    step = len(items) / limit
    picked = [items[int(i * step)] for i in range(limit - 1)]
    picked.append(items[-1])
    return picked


def curve_payload(curve: Sequence[EquityPoint]) -> list[dict[str, Any]]:
    return [{"at": p.at.isoformat(), "equity": float(p.equity)} for p in downsample(curve, MAX_CURVE_POINTS)]


def symbol_payload(
    symbol: str, result: BacktestResult, source: str, notes: Sequence[str], timeframe: Timeframe
) -> dict[str, Any]:
    metrics = compute_metrics(result)
    avoided = summarize_avoided(result.decisions, result.risk_events)
    trips = result.round_trips[-MAX_TRIPS:]
    return {
        "symbol": symbol,
        "timeframe": timeframe.value,
        "data_source": source,
        "data_notes": list(notes),
        "candles": result.candles,
        "period": {"start": result.period_start.isoformat(), "end": result.period_end.isoformat()},
        "metrics": metrics.model_dump(mode="json"),
        "equity_curve": curve_payload(result.equity_curve),
        "trades": [
            {
                "strategy": t.strategy,
                "opened_at": t.opened_at.isoformat(),
                "closed_at": t.closed_at.isoformat(),
                "entry_price": str(t.entry_price),
                "quantity": str(t.quantity),
                "pnl": str(t.pnl),
                "fees": str(t.fees),
                "r_multiple": str(t.r_multiple),
                "exits": list(t.labels),
            }
            for t in trips
        ],
        "trades_total": len(result.round_trips),
        "avoided": avoided.model_dump(mode="json"),
        "halted": result.halted,
        "walk_forward": None,
    }


async def run_job(params: dict[str, Any], *, cache_dir: Path, use_db: bool) -> dict[str, Any]:
    timeframe = Timeframe(params["timeframe"])
    symbols: list[str] = params["symbols"]
    names: list[str] = params["strategies"]
    fixed = {k: Decimal(str(v)) for k, v in params["params"].items()}
    start = datetime.fromisoformat(params["start"])
    end = datetime.fromisoformat(params["end"])
    settings = BacktestSettings(
        initial_capital=Decimal(str(params["capital"])),
        maker_fee_rate=Decimal(str(params["maker_fee"])),
        slippage_bps=Decimal(str(params["slippage_bps"])),
        latency_candles=int(params["latency_candles"]),
        risk=RiskConfig(taker_fee_rate=Decimal(str(params["taker_fee"]))),
    )
    share = 100.0 / len(symbols)
    results: list[dict[str, Any]] = []
    for index, symbol in enumerate(symbols):
        base = index * share

        def progress(
            fraction: float, stage: str, message: str, base: float = base, symbol: str = symbol
        ) -> None:
            emit(
                type="progress",
                stage=stage,
                percent=round(base + share * fraction, 1),
                message=message,
                symbol=symbol,
            )

        progress(0.0, "loading", f"Baixando velas de {symbol}")
        loaded = await load_candles(symbol, timeframe, start, end, cache_dir=cache_dir, use_db=use_db)
        if len(loaded.candles) < 2:
            raise ValueError(f"{symbol}: sem velas suficientes ({loaded.source})")
        progress(0.2, "simulating", f"Simulando {symbol} ({len(loaded.candles)} velas)")
        rules = default_rules(symbol)
        now = datetime.now(UTC)
        result = await BacktestEngine(
            build_strategies(names, fixed), settings, rules=rules, clock=frozen_clock(now)
        ).run(loaded.candles)
        payload = symbol_payload(symbol, result, loaded.source, loaded.notes, timeframe)
        if params.get("walk_forward"):
            progress(0.7, "walk_forward", f"Teste em janelas de {symbol}")
            payload["walk_forward"] = await _walk_forward(params, loaded.candles, names, settings, rules, now)
        results.append(payload)
        progress(1.0, "done", f"{symbol} concluído")
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "assumptions": {
            "capital": str(settings.initial_capital),
            "taker_fee": str(settings.risk.taker_fee_rate),
            "maker_fee": str(settings.maker_fee_rate),
            "slippage_bps": str(settings.slippage_bps),
            "latency_candles": settings.latency_candles,
            "timeframe": timeframe.value,
            "strategies": names,
            "period": {"start": start.isoformat(), "end": end.isoformat()},
        },
        "results": results,
    }


async def _walk_forward(
    params: dict[str, Any],
    candles: Any,
    names: list[str],
    settings: BacktestSettings,
    rules: Any,
    now: datetime,
) -> dict[str, Any]:
    config = WalkForwardConfig(train_days=int(params["train_days"]), test_days=int(params["test_days"]))
    grid = parse_grid(params.get("grid") or DEFAULT_GRID)
    try:
        wf = await walk_forward(
            candles,
            lambda p: build_strategies(names, p),
            grid,
            settings,
            config,
            rules=rules,
            clock=lambda: now,
        )
    except NotEnoughCandlesError as error:
        return {"error": str(error)}
    return {
        "config": config.model_dump(mode="json"),
        "grid": {k: [str(v) for v in values] for k, values in grid.values.items()},
        "windows": [
            {
                "index": w.index,
                "train_start": w.train_start.isoformat(),
                "train_end": w.train_end.isoformat(),
                "test_start": w.test_start.isoformat(),
                "test_end": w.test_end.isoformat(),
                "selected": None if w.selected is None else {k: str(v) for k, v in w.selected.items()},
                "train_return": None if w.train_metrics is None else w.train_metrics.total_return,
                "test_return": None if w.test_metrics is None else w.test_metrics.total_return,
                "test_trades": None if w.test_metrics is None else w.test_metrics.trades,
            }
            for w in wf.windows
        ],
        "out_of_sample": wf.out_of_sample.model_dump(mode="json"),
        "out_of_sample_curve": curve_payload(wf.out_of_sample_curve),
        "assessment": wf.assessment.model_dump(mode="json"),
        "verdict_text": VERDICTS.get(wf.assessment.verdict, wf.assessment.verdict),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m engine.runner.backtest_job", description=__doc__)
    parser.add_argument("--params", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, default=Path("data/cache"))
    parser.add_argument("--no-db", action="store_true")
    parser.add_argument("--max-memory-mb", type=float, default=0)
    args = parser.parse_args(argv)
    if args.max_memory_mb > 0:
        start_memory_guard(args.max_memory_mb)
    try:
        params = json.loads(args.params.read_text(encoding="utf-8"))
        report = asyncio.run(run_job(params, cache_dir=args.cache_dir, use_db=not args.no_db))
        args.out.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    except Exception as error:
        emit(type="error", code="failed", message=f"{type(error).__name__}: {str(error)[:300]}")
        return EXIT_FAILED
    emit(type="done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
