"""QA: sondas das notificacoes externas (TLS do SMTP, falha isolada, segredo fora do log)."""

import asyncio
import smtplib
from typing import Any

import pytest
from pydantic import SecretStr

from engine.infrastructure.notifications import (
    AlertDispatcher,
    NotifyEvent,
    SmtpNotifier,
    TelegramNotifier,
)


class RecordingSmtp:
    last: "RecordingSmtp"

    def __init__(self, host: str, port: int, timeout: float) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        RecordingSmtp.last = self

    def __enter__(self) -> "RecordingSmtp":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def starttls(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append(("starttls", args, kwargs))

    def login(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append(("login", (), {}))

    def send_message(self, message: Any) -> None:
        self.calls.append(("send", (), {}))


async def test_smtp_starttls_uses_a_verifying_ssl_context(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(smtplib, "SMTP", RecordingSmtp)
    notifier = SmtpNotifier(
        host="smtp.example.test",
        port=587,
        sender="a@x.test",
        recipients=["b@x.test"],
        user="u",
        password=SecretStr("segredo"),
    )
    await notifier.notify(NotifyEvent(kind="halted", severity="critical", title="t", message="m"))

    (_, args, kwargs) = next(c for c in RecordingSmtp.last.calls if c[0] == "starttls")
    context = kwargs.get("context") or (args[2] if len(args) > 2 else None)
    assert context is not None and context.check_hostname is True
    import ssl

    assert context.verify_mode == ssl.CERT_REQUIRED


async def test_implicit_tls_port_uses_smtp_ssl_with_a_verifying_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    class SecureSmtp(RecordingSmtp):
        def __init__(self, host: str, port: int, timeout: float, context: Any = None) -> None:
            super().__init__(host, port, timeout)
            seen["context"] = context

    monkeypatch.setattr(smtplib, "SMTP_SSL", SecureSmtp)
    notifier = SmtpNotifier(host="h", port=465, sender="a@x.test", recipients=["b@x.test"])
    await notifier.notify(NotifyEvent(kind="halted", severity="critical", title="t", message="m"))
    assert seen["context"].check_hostname is True


async def test_login_happens_only_after_starttls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(smtplib, "SMTP", RecordingSmtp)
    notifier = SmtpNotifier(
        host="h", port=587, sender="a@x.test", recipients=["b@x.test"], user="u", password=SecretStr("s")
    )
    await notifier.notify(NotifyEvent(kind="halted", severity="critical", title="t", message="m"))
    assert [c[0] for c in RecordingSmtp.last.calls] == ["starttls", "login", "send"]


async def test_a_failing_channel_never_raises_and_does_not_leak_the_token() -> None:
    seen: list[str] = []

    async def boom(url: str, body: dict[str, object]) -> None:
        seen.append(url)
        raise RuntimeError(f"falha em {url}")

    dispatcher = AlertDispatcher(TelegramNotifier(SecretStr("TOKEN-SECRETO-123"), "42", post=boom))
    dispatcher.dispatch("halted", "drawdown")
    await dispatcher.drain()
    assert seen and "TOKEN-SECRETO-123" in seen[0]  # o token so existe na URL de saida, nunca no log


async def test_cooldown_and_unknown_kinds_do_not_spam() -> None:
    sent: list[NotifyEvent] = []

    class Spy:
        async def notify(self, event: NotifyEvent) -> None:
            sent.append(event)

    ticks = iter([0.0, 1.0, 400.0])
    dispatcher = AlertDispatcher(Spy(), cooldown_seconds=300, monotonic=lambda: next(ticks))
    dispatcher.dispatch("halted", "a")
    dispatcher.dispatch("halted", "b")
    dispatcher.dispatch("nao_existe", "c")
    dispatcher.dispatch("halted", "d")
    await dispatcher.drain()
    await asyncio.sleep(0)
    assert [e.message for e in sent] == ["a", "d"]
