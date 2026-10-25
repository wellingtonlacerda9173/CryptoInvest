"""Pilha real para o E2E do painel: banco `*_test` descartavel, API (uvicorn), Redis real (prefixo proprio) e
um robo de paper de mentira (o mesmo `PaperRobot`, com diario em memoria) que consome os comandos assinados.

Uso: `uv run python -m tests.e2e_stack --ready-file R --stop-file S [--api-port 8765]`
Grava `--ready-file` com JSON `{"ready": true, ...}` e roda ate receber Ctrl+C/SIGTERM; ao sair derruba tudo.
"""

import argparse
import asyncio
import contextlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import traceback
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from api.cli import init_secrets
from engine.application.ai.lesson_job import NewLesson
from engine.application.ai.lessons import (
    LessonReport,
    Observation,
    build_summary,
    deterministic_report,
)
from engine.domain.candle import Candle
from engine.domain.types import Timeframe
from engine.infrastructure.config.settings import load_settings
from engine.infrastructure.persistence.candle_repository import CandleRepository
from engine.infrastructure.persistence.lesson_repository import SqlLessonStore
from engine.infrastructure.persistence.models import DecisionRow, TradeRow
from engine.infrastructure.persistence.session import create_engine, create_session_factory
from engine.runner.command_channel import RedisCommandSource, load_command_key
from engine.runner.paper_control import CommandQueue
from engine.runner.paper_events import RedisEventBus, RedisKeys
from tests.api_support import GOOD_PASSWORD
from tests.integration.api.runner_support import RUN_ID, redis_url_of, seed_once
from tests.integration.disposable_db import assert_disposable_database, disposable_database, upgrade_database
from tests.unit.runner.fakes import FakeClock, Harness, MemoryJournal

ADMIN_USER = "admin.e2e"


class _FakeBinance(BaseHTTPRequestHandler):
    """So `apiRestrictions`: chave de leitura+trade, sem saque e com restricao de IP."""

    def do_GET(self) -> None:
        body = json.dumps(
            {
                "enableReading": True,
                "enableSpotAndMarginTrading": True,
                "enableWithdrawals": False,
                "ipRestrict": True,
            }
        ).encode()  # fmt: skip
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


_E2E_TRADES = (
    (20, "115.5", "113", "stop_loss", "-25.2", "-1.0"),
    (8, "118", "121", "take_profit", "29.8", "1.2"),
)


def _seed_screens(url: object) -> None:
    """Dados extras das telas Mercado/Decisoes/Operacoes (so no banco `*_test` descartavel desta pilha):
    uma decisao com regime, notas e parecer de IA, e uma perda e um ganho em precos coerentes com as velas."""

    async def go() -> None:
        engine = create_engine(url)  # type: ignore[arg-type]
        try:
            now = datetime.now(UTC).replace(microsecond=0)
            async with create_session_factory(engine).begin() as db:
                db.add(
                    DecisionRow(
                        decision_key="e2e-rich", run_id=RUN_ID, decided_at=now - timedelta(seconds=30),
                        symbol="BTC/USDT", action="skip", traded=False, code="reward_risk",
                        reason="reward_risk: tecnico", strategy="elephant", confidence=0.72,
                        signal_id="e2e-sig",
                        signal={
                            "symbol": "BTC/USDT", "strategy": "elephant", "entry": "123", "stop_loss": "120",
                            "take_profit": "127", "regime": "tendência de alta",
                            "scores": [
                                {"strategy": "elephant", "score": 0.72},
                                {"strategy": "ema", "score": 0.41},
                            ],
                            "ai_opinion": "Contexto favorável, mas o ganho é pequeno para o risco.",
                        },
                        context={},
                    )
                )  # fmt: skip
                for index, (hours, entry, exit_, reason, pnl, r) in enumerate(_E2E_TRADES):
                    closed = now - timedelta(hours=hours)
                    db.add(
                        TradeRow(
                            trade_id=f"e2e{index}", run_id=RUN_ID, symbol="BTC/USDT", quantity=Decimal(10),
                            entry_price=Decimal(entry), exit_price=Decimal(exit_), entry_fee=Decimal("0.1"),
                            exit_fee=Decimal("0.1"), pnl=Decimal(pnl), opened_at=closed - timedelta(hours=5),
                            closed_at=closed, exit_reason=reason, strategy="elephant",
                            risk_amount=Decimal("25"), r_multiple=Decimal(r),
                        )
                    )  # fmt: skip
        finally:
            await engine.dispose()

    asyncio.run(go())


def _seed_lessons(url: object) -> None:
    """Duas licoes PENDENTES (so neste banco `*_test`): uma com texto da IA contendo marcacao HTML (o painel
    precisa exibi-la como texto) e uma so com estatisticas."""

    async def go() -> None:
        engine = create_engine(url)  # type: ignore[arg-type]
        try:
            store = SqlLessonStore(create_session_factory(engine))
            now = datetime.now(UTC).replace(microsecond=0)
            start = now - timedelta(days=7)
            summary = build_summary([], {"ai_veto": 2, "selector_idle": 5}, {}, start=start, end=now)
            # `model_construct` pula a sanitizacao: o banco guarda marcacao crua, a API limpa ao ler
            observation = Observation.model_construct(
                text="Ao vivo a <b>elephant</b> perdeu mais em mercado lateral.",
                evidence="4 perdas em 6 operações, -4,00R",
                confidence="low",
            )
            ai_report = LessonReport.model_construct(
                summary="Semana de poucas operações; a elephant perdeu em mercado lateral.",
                observations=(observation,),
            )
            for age, source, report in (
                (timedelta(hours=1), "ai", ai_report),
                (timedelta(days=7, hours=1), "deterministic", deterministic_report(summary)),
            ):
                await store.add(
                    NewLesson(
                        created_at=now - age,
                        period_start=start - age,
                        period_end=now - age,
                        source=source,  # type: ignore[arg-type]
                        model="claude-haiku-5-5" if source == "ai" else None,
                        prompt_version="lessons-v1" if source == "ai" else "deterministic-v1",
                        summary=summary,
                        report=report,
                    )
                )
        finally:
            await engine.dispose()

    asyncio.run(go())


SYNTHETIC_SYMBOL = "SOL/USDT"


def _synthetic_candles(now: datetime, days: int = 21) -> list[Candle]:
    """Velas 1h SINTETICAS (onda + barras elefante periodicas) cobrindo os ultimos `days` dias e a frente:
    o job de backtest do E2E le o banco `*_test` e nunca precisa da Binance."""
    import math

    step = timedelta(hours=1)
    first = now.replace(minute=0, second=0, microsecond=0) - timedelta(days=days)
    candles: list[Candle] = []
    price = 100.0
    for i in range((days + 2) * 24):
        drift = math.sin(i / 9) * 0.35 + 0.02
        body = 2.4 if i % 37 == 36 else drift
        open_, close = price, max(price + body, 1.0)
        high, low = max(open_, close) + 0.3, min(open_, close) - 0.3
        volume = 900.0 if i % 37 == 36 else 100.0 + (i % 5) * 7
        candles.append(
            Candle(
                symbol=SYNTHETIC_SYMBOL, timeframe=Timeframe.H1, open_time=first + step * i,
                open=Decimal(f"{open_:.4f}"), high=Decimal(f"{high:.4f}"), low=Decimal(f"{low:.4f}"),
                close=Decimal(f"{close:.4f}"), volume=Decimal(f"{volume:.2f}"), closed=True,
            )
        )  # fmt: skip
        price = close
    return candles


def _seed_candles(url: object) -> None:
    async def go() -> None:
        engine = create_engine(url)  # type: ignore[arg-type]
        try:
            await CandleRepository(create_session_factory(engine)).upsert_many(
                _synthetic_candles(datetime.now(UTC))
            )
        finally:
            await engine.dispose()

    asyncio.run(go())


def _env(secrets_dir: Path, database: str, args: argparse.Namespace, prefix: str, url: str) -> dict[str, str]:
    return {
        **os.environ,
        "SECRETS_DIR": str(secrets_dir),
        "DATABASE_NAME": database,
        "API_PANEL_ORIGINS": json.dumps([args.panel_origin]),
        "API_COOKIE_SECURE": "false",
        "API_REDIS_URL": url,
        "API_REDIS_PREFIX": prefix,
        "API_ARGON2_TIME_COST": "1",
        "API_ARGON2_MEMORY_KIB": "1024",
        "API_LOGIN_USER_DELAY_BASE_SECONDS": "0.01",
        "API_LOGIN_USER_DELAY_CAP_SECONDS": "0.05",
        "API_COMMAND_PUMP_SECONDS": "0.5",
        "API_BACKTEST_CACHE_DIR": str(secrets_dir.parent / "cache"),
        "API_BINANCE_API_ORIGIN": f"http://127.0.0.1:{args.api_port + 1}",
    }


def _wait_stop(api: subprocess.Popen[bytes], stop_file: Path) -> None:
    import time

    while api.poll() is None and not stop_file.exists():
        time.sleep(0.5)


async def _serve(
    url: str, keys: RedisKeys, key: bytes, queue_dir: Path, api: subprocess.Popen[bytes], stop_file: Path
) -> None:
    stop = asyncio.Event()
    task = asyncio.create_task(_run_robot(url, keys, key, queue_dir, stop))
    await asyncio.to_thread(_wait_stop, api, stop_file)
    stop.set()
    await task


async def _run_robot(url: str, keys: RedisKeys, key: bytes, queue_dir: Path, stop: asyncio.Event) -> None:
    harness = await Harness(journal=MemoryJournal(RUN_ID), clock=FakeClock(datetime.now(UTC))).start()
    bus = RedisEventBus(url, keys=keys, signing_key=key)
    source = RedisCommandSource.from_url(url, key, keys=keys)
    robot = harness.robot
    robot.bus, robot.commands, robot.signed_commands = bus, CommandQueue(queue_dir), source
    await robot._publish_state("start")
    try:
        while not stop.is_set():
            harness.clock.set(datetime.now(UTC))
            try:
                await robot.housekeeping()
            except Exception:
                traceback.print_exc()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), 0.3)
    finally:
        await bus.close()
        await source.close()


def _wait_port(port: int, api: subprocess.Popen[bytes]) -> None:
    import socket
    import time

    deadline = time.monotonic() + 180
    while time.monotonic() < deadline and api.poll() is None:
        with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", port), timeout=1):
            return
        time.sleep(0.5)
    raise RuntimeError("a API nao abriu a porta a tempo")


def run(args: argparse.Namespace) -> None:
    settings = load_settings()
    url = redis_url_of(settings)
    prefix = f"cryptoinvest:e2e:{secrets.token_hex(4)}"
    work = Path(tempfile.mkdtemp(prefix="ci-e2e-"))
    secrets_dir = work / "secrets"
    secrets_dir.mkdir()
    shutil.copy(Path(os.environ.get("SECRETS_DIR", "./secrets")) / "database_password", secrets_dir)
    init_secrets(secrets_dir)
    key = load_command_key(secrets_dir)
    assert key is not None
    api: subprocess.Popen[bytes] | None = None
    binance = ThreadingHTTPServer(("127.0.0.1", args.api_port + 1), _FakeBinance)
    threading.Thread(target=binance.serve_forever, daemon=True).start()
    try:
        with disposable_database(settings, "e2e") as db_url:
            name = assert_disposable_database(db_url)
            upgrade_database(db_url)
            seed_once(db_url)()
            _seed_screens(db_url)
            _seed_candles(db_url)
            _seed_lessons(db_url)
            env = _env(secrets_dir, name, args, prefix, url)
            cli = [sys.executable, "-m", "api.cli", "create-admin", "--password-stdin"]
            created = subprocess.run(  # noqa: S603
                [*cli, "--username", ADMIN_USER],
                input=GOOD_PASSWORD + chr(10),
                env=env,
                capture_output=True,
                text=True,
                check=True,
                timeout=120,
            )
            token = next(
                line.split(": ", 1)[1].split("\n")[0].strip()
                for line in created.stdout.splitlines()
                if line.startswith("TOKEN DE ATIVACAO")
            )
            api = subprocess.Popen(  # noqa: S603
                [sys.executable, "-m", "api.serve", "--port", str(args.api_port)], env=env
            )
            _wait_port(args.api_port, api)
            ready = json.dumps(
                {"ready": True, "username": ADMIN_USER, "password": GOOD_PASSWORD, "activation": token}
            )
            Path(args.ready_file).write_text(ready, encoding="utf-8")
            asyncio.run(_serve(url, RedisKeys(prefix), key, work / "queue", api, Path(args.stop_file)))
    finally:
        if api is not None and api.poll() is None:
            api.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                api.wait(10)
            if api.poll() is None:
                api.kill()
        binance.shutdown()
        shutil.rmtree(work, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-port", type=int, default=8765)
    parser.add_argument("--ready-file", required=True, help="recebe o JSON de credenciais quando pronta")
    parser.add_argument("--stop-file", required=True, help="a pilha encerra e limpa o banco quando existir")
    parser.add_argument("--panel-origin", default="http://127.0.0.1:5198")
    run(parser.parse_args())


if __name__ == "__main__":
    main()
