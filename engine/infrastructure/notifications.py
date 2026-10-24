"""Alertas externos opcionais (Telegram e e-mail), DESLIGADOS por padrao.

Credenciais so de arquivos secretos em SECRETS_DIR (`telegram_bot_token`, `smtp_password`); nada vai para
o repositorio nem para o ambiente. Uma falha de notificacao NUNCA afeta o robo: toda entrega roda com tempo
limite, a excecao e engolida (so um log sem segredo) e cada tipo de alerta tem intervalo minimo.
"""

import asyncio
import smtplib
import ssl
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Literal, Protocol

import aiohttp
import structlog
from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from engine.infrastructure.config.logging import scrub_text

log = structlog.get_logger()

# alertas do runner que merecem sair do painel: o resto fica no log e no painel
NOTIFY_KINDS: dict[str, tuple[Literal["critical", "warning"], str]] = {
    "halted": ("critical", "Robo DESLIGADO por drawdown"),
    "halt_inherited": ("critical", "Robo iniciou desligado (drawdown do run anterior)"),
    "persistence_down": ("critical", "Banco de dados indisponivel (FAILED_PERSISTENCE)"),
    "stops_blind_prolonged": ("critical", "Stops cegos por tempo prolongado com posicao aberta"),
    "daily_loss_pause": ("warning", "Perda diaria atingida: sem novas operacoes ate 00:00 UTC"),
}
DEFAULT_COOLDOWN_SECONDS = 300.0
DELIVERY_TIMEOUT_SECONDS = 10.0
IMPLICIT_TLS_PORT = 465


class NotifyEvent(BaseModel):
    kind: str
    severity: Literal["critical", "warning"]
    title: str
    message: str
    run_id: str | None = None
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def text(self) -> str:
        run = f" [{self.run_id}]" if self.run_id else ""
        return f"CryptoInvest{run}\n{self.title}\n{self.message}\n{self.at:%Y-%m-%d %H:%M:%S} UTC"


class Notifier(Protocol):
    async def notify(self, event: NotifyEvent) -> None: ...


class NullNotifier:
    async def notify(self, event: NotifyEvent) -> None:
        return None


class NotifySettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NOTIFY_", env_file=".env", extra="ignore", frozen=True)

    enabled: bool = False
    telegram_chat_id: str | None = None
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_from: str | None = None
    smtp_to: list[str] = []
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS


Post = Callable[[str, dict[str, object]], Awaitable[None]]


async def _http_post(url: str, body: dict[str, object]) -> None:
    timeout = aiohttp.ClientTimeout(total=DELIVERY_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout) as session, session.post(url, json=body) as response:
        response.raise_for_status()


class TelegramNotifier:
    def __init__(self, token: SecretStr, chat_id: str, post: Post = _http_post) -> None:
        self._token = token
        self._chat_id = chat_id
        self._post = post

    async def notify(self, event: NotifyEvent) -> None:
        url = f"https://api.telegram.org/bot{self._token.get_secret_value()}/sendMessage"
        await self._post(url, {"chat_id": self._chat_id, "text": event.text()})


SendMail = Callable[[EmailMessage], None]


class SmtpNotifier:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        sender: str,
        recipients: Sequence[str],
        user: str | None = None,
        password: SecretStr | None = None,
        send: SendMail | None = None,
    ) -> None:
        self._host, self._port, self._sender = host, port, sender
        self._recipients = list(recipients)
        self._user, self._password = user, password
        self._send = send or self._deliver

    def _deliver(self, message: EmailMessage) -> None:
        # contexto padrao: valida cadeia do certificado E nome do servidor (senha SMTP nao vai a um MITM)
        context = ssl.create_default_context()
        if self._port == IMPLICIT_TLS_PORT:
            with smtplib.SMTP_SSL(
                self._host, self._port, timeout=DELIVERY_TIMEOUT_SECONDS, context=context
            ) as secure:
                self._login_and_send(secure, message)
            return
        with smtplib.SMTP(self._host, self._port, timeout=DELIVERY_TIMEOUT_SECONDS) as smtp:
            smtp.starttls(context=context)
            self._login_and_send(smtp, message)

    def _login_and_send(self, smtp: smtplib.SMTP, message: EmailMessage) -> None:
        if self._user and self._password is not None:
            smtp.login(self._user, self._password.get_secret_value())
        smtp.send_message(message)

    async def notify(self, event: NotifyEvent) -> None:
        message = EmailMessage()
        message["Subject"] = f"[CryptoInvest] {event.title}"[:200]
        message["From"] = self._sender
        message["To"] = ", ".join(self._recipients)
        message.set_content(event.text())
        await asyncio.to_thread(self._send, message)


class CompositeNotifier:
    def __init__(self, notifiers: Sequence[Notifier]) -> None:
        self._notifiers = list(notifiers)

    async def notify(self, event: NotifyEvent) -> None:
        results = await asyncio.gather(*(n.notify(event) for n in self._notifiers), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                log.warning("notification_channel_failed", error=type(result).__name__)


class AlertDispatcher:
    """Ponte do robo: filtra os tipos relevantes, aplica intervalo minimo e NUNCA levanta nem bloqueia."""

    def __init__(
        self,
        notifier: Notifier,
        *,
        run_id: str | None = None,
        cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
        timeout_seconds: float = DELIVERY_TIMEOUT_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._notifier = notifier
        self._run_id = run_id
        self._cooldown = cooldown_seconds
        self._timeout = timeout_seconds
        self._monotonic = monotonic
        self._last: dict[str, float] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    def dispatch(self, kind: str, message: str) -> None:
        """Agenda a entrega em segundo plano; devolve na hora."""
        meta = NOTIFY_KINDS.get(kind)
        if meta is None:
            return
        now = self._monotonic()
        previous = self._last.get(kind)
        if previous is not None and now - previous < self._cooldown:
            return
        self._last[kind] = now
        event = NotifyEvent(
            kind=kind, severity=meta[0], title=meta[1], message=scrub_text(message)[:500], run_id=self._run_id
        )
        task = asyncio.get_running_loop().create_task(self._deliver(event))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _deliver(self, event: NotifyEvent) -> None:
        try:
            await asyncio.wait_for(self._notifier.notify(event), self._timeout)
        except Exception as error:
            log.warning("notification_failed", kind=event.kind, error=type(error).__name__)

    async def drain(self, wait_seconds: float = 3.0) -> None:
        if self._tasks:
            await asyncio.wait(list(self._tasks), timeout=wait_seconds)


def _read_secret(directory: Path | None, name: str) -> SecretStr | None:
    if directory is None:
        return None
    try:
        value = (directory / name).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return SecretStr(value) if value else None


def build_notifier(settings: NotifySettings, secrets_dir: Path | None) -> Notifier:
    """Desligado por padrao; so monta os canais que tem configuracao E credencial completas."""
    if not settings.enabled:
        return NullNotifier()
    channels: list[Notifier] = []
    token = _read_secret(secrets_dir, "telegram_bot_token")
    if token is not None and settings.telegram_chat_id:
        channels.append(TelegramNotifier(token, settings.telegram_chat_id))
    if settings.smtp_host and settings.smtp_from and settings.smtp_to:
        channels.append(
            SmtpNotifier(
                host=settings.smtp_host,
                port=settings.smtp_port,
                sender=settings.smtp_from,
                recipients=settings.smtp_to,
                user=settings.smtp_user,
                password=_read_secret(secrets_dir, "smtp_password"),
            )
        )
    if not channels:
        log.warning("notifications_enabled_without_channel")
        return NullNotifier()
    return CompositeNotifier(channels)
