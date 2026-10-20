"""Configuracao do run de paper e caminhos de trabalho (estado, comandos, relatorios)."""

import hashlib
import json
import re
from decimal import Decimal
from pathlib import Path
from typing import Self

from pydantic import Field, model_validator

from engine.domain.base import DomainModel
from engine.domain.risk import RiskConfig
from engine.domain.types import Fee, StrictDecimal, Symbol, Timeframe, Volume
from engine.infrastructure.paper import PaperConfig
from engine.runner.paper_control import ensure_private_dir

DEFAULT_WS_URL = "wss://stream.binance.com:443"
DEFAULT_CALENDAR = Path("config/volatility_events.json")
DEFAULT_DATA_DIR = Path("data/paper")
LATEST_POINTER = "latest"
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class PaperRunConfig(DomainModel):
    """Premissas do run. Os campos de `trading_fields` entram no hash: mudar um deles e outro run.

    `allow_open_calendar` fica FORA do hash de proposito: reiniciar o run sem a flag precisa ser possivel, mas
    nao apaga o passado. O relatorio considera o run contaminado se QUALQUER `paper_start` do `audit_log` ou
    QUALQUER decisao (`context.calendar_open`) usou a flag (ver `paper_report_data.used_open_calendar`).
    """

    symbols: tuple[Symbol, ...] = Field(default=("BTC/USDT", "ETH/USDT"), min_length=1)
    timeframe: Timeframe = Timeframe.H1
    capital: Volume = Decimal(10_000)
    strategies: tuple[str, ...] = ("elephant", "ema")
    params: dict[str, StrictDecimal] = Field(default_factory=dict)
    risk: RiskConfig = RiskConfig()
    maker_fee: Fee = Decimal("0.001")
    slippage_bps: StrictDecimal = Field(default=Decimal(5), ge=0)
    latency_ms: int = Field(default=100, ge=0)
    max_stop_blind_seconds: int = Field(default=30, gt=0)
    max_book_age_seconds: int = Field(default=10, gt=0)

    allow_open_calendar: bool = False
    history_window: int = Field(default=400, ge=60)
    heartbeat_seconds: int = Field(default=30, gt=0)
    equity_seconds: int = Field(default=60, gt=0)
    reconcile_seconds: int = Field(default=300, gt=0)
    blind_alert_seconds: int = Field(default=300, gt=0)
    max_eval_lag_seconds: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _check_symbols(self) -> Self:
        if len(set(self.symbols)) != len(self.symbols):
            raise ValueError("symbols must be unique")
        return self

    @property
    def eval_lag_seconds(self) -> int:
        """Atraso maximo entre o fechamento da vela e a avaliacao para ainda poder abrir posicao."""
        if self.max_eval_lag_seconds is not None:
            return self.max_eval_lag_seconds
        return min(self.timeframe.seconds // 4, 120)

    def gateway_config(self) -> PaperConfig:
        return PaperConfig(
            symbols=self.symbols,
            initial_quote=self.capital,
            maker_fee=self.maker_fee,
            taker_fee=self.risk.taker_fee_rate,
            slippage_bps=self.slippage_bps,
            latency_ms=self.latency_ms,
            max_stop_blind_seconds=self.max_stop_blind_seconds,
            max_book_age_seconds=self.max_book_age_seconds,
        )

    def trading_fields(self) -> dict[str, object]:
        return self.model_dump(
            mode="json",
            include={
                "symbols",
                "timeframe",
                "capital",
                "strategies",
                "params",
                "risk",
                "maker_fee",
                "slippage_bps",
                "latency_ms",
                "max_stop_blind_seconds",
                "max_book_age_seconds",
            },
        )

    def config_hash(self) -> str:
        raw = json.dumps(self.trading_fields(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()[:32]


def validate_run_id(run_id: str) -> str:
    if not _RUN_ID.fullmatch(run_id):
        raise ValueError(f"run_id invalido: {run_id!r} (letras, numeros, '.', '_' e '-', ate 64 caracteres)")
    return run_id


class RunPaths:
    """`<data_dir>/<run_id>/` guarda status, configuracao e a fila de comandos do run."""

    def __init__(self, data_dir: Path, run_id: str) -> None:
        self.root = data_dir / validate_run_id(run_id)
        self.commands = self.root / "commands"
        self.outbox = self.root / "outbox"
        self.status = self.root / "status.json"
        self.config = self.root / "config.json"
        self.lock = self.root / "robot.lock"

    def ensure(self) -> None:
        ensure_private_dir(self.root)
        ensure_private_dir(self.commands)
        ensure_private_dir(self.outbox)


def write_latest(data_dir: Path, run_id: str) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / LATEST_POINTER).write_text(run_id, encoding="utf-8")


def read_latest(data_dir: Path) -> str | None:
    pointer = data_dir / LATEST_POINTER
    return pointer.read_text(encoding="utf-8").strip() or None if pointer.exists() else None
