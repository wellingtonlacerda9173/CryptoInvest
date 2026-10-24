"""Alertas externos: desligados por padrao, segredos so de arquivo, falha nunca afeta o robo."""

import asyncio
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from engine.infrastructure.notifications import (
    NOTIFY_KINDS,
    AlertDispatcher,
    CompositeNotifier,
    NotifyEvent,
    NotifySettings,
    NullNotifier,
    SmtpNotifier,
    TelegramNotifier,
    build_notifier,
)

TOKEN = "123456:ABC-secret-token-value"  # noqa: S105
PASSWORD = "senha-super-secreta-smtp"  # noqa: S105


def settings(**overrides: Any) -> NotifySettings:
    return NotifySettings(
        _env_file=None,  # type: ignore[call-arg]
        **overrides,
    )


def secrets(tmp_path: Path) -> Path:
    (tmp_path / "telegram_bot_token").write_text(TOKEN + "\n", encoding="utf-8")
    (tmp_path / "smtp_password").write_text(PASSWORD, encoding="utf-8")
    return tmp_path


def event(kind: str = "halted") -> NotifyEvent:
    severity, title = NOTIFY_KINDS[kind]
    return NotifyEvent(kind=kind, severity=severity, title=title, message="detalhe", run_id="paper-1")


def test_everything_is_off_by_default_even_with_secrets_and_channels_configured(tmp_path: Path) -> None:
    configured = settings(telegram_chat_id="1", smtp_host="h", smtp_from="a@b.c", smtp_to=["x@y.z"])
    assert configured.enabled is False
    assert isinstance(build_notifier(configured, secrets(tmp_path)), NullNotifier)


def test_enabled_without_any_complete_channel_stays_off(tmp_path: Path) -> None:
    assert isinstance(build_notifier(settings(enabled=True), tmp_path), NullNotifier)
    assert isinstance(build_notifier(settings(enabled=True, telegram_chat_id="1"), tmp_path), NullNotifier)
    assert isinstance(
        build_notifier(settings(enabled=True, smtp_host="h", smtp_from="a@b.c"), tmp_path), NullNotifier
    )


def test_channels_are_built_only_with_their_credentials(tmp_path: Path) -> None:
    only_telegram = build_notifier(settings(enabled=True, telegram_chat_id="42"), secrets(tmp_path))
    assert isinstance(only_telegram, CompositeNotifier) and len(only_telegram._notifiers) == 1
    both = build_notifier(
        settings(enabled=True, telegram_chat_id="42", smtp_host="h", smtp_from="a@b.c", smtp_to=["x@y.z"]),
        secrets(tmp_path),
    )
    assert isinstance(both, CompositeNotifier) and len(both._notifiers) == 2


def test_secrets_are_never_fields_of_the_environment_settings() -> None:
    names = set(NotifySettings.model_fields)
    assert not {n for n in names if "token" in n or "password" in n or "secret" in n}


async def test_telegram_posts_the_alert_text_to_the_bot_api() -> None:
    sent: list[tuple[str, dict[str, object]]] = []

    async def post(url: str, body: dict[str, object]) -> None:
        sent.append((url, body))

    await TelegramNotifier(SecretStr(TOKEN), "42", post=post).notify(event())
    [(url, body)] = sent
    assert url == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert body["chat_id"] == "42" and "Robo DESLIGADO por drawdown" in str(body["text"])
    assert TOKEN not in str(body["text"])


async def test_smtp_builds_a_message_without_leaking_the_password() -> None:
    messages: list[EmailMessage] = []
    notifier = SmtpNotifier(
        host="smtp.local",
        port=587,
        sender="robo@exemplo.com",
        recipients=["a@x.com", "b@x.com"],
        user="robo",
        password=SecretStr(PASSWORD),
        send=messages.append,
    )
    await notifier.notify(event("persistence_down"))
    [message] = messages
    assert message["From"] == "robo@exemplo.com" and message["To"] == "a@x.com, b@x.com"
    assert "FAILED_PERSISTENCE" in message["Subject"] and "detalhe" in message.get_content()
    assert PASSWORD not in message.as_string()


async def test_one_failing_channel_does_not_block_the_other() -> None:
    delivered: list[str] = []

    class Ok:
        async def notify(self, e: NotifyEvent) -> None:
            delivered.append(e.kind)

    class Broken:
        async def notify(self, e: NotifyEvent) -> None:
            raise ConnectionError("fora do ar")

    await CompositeNotifier([Broken(), Ok()]).notify(event())
    assert delivered == ["halted"]


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


async def test_dispatch_filters_kinds_and_applies_a_cooldown() -> None:
    seen: list[NotifyEvent] = []

    class Recorder:
        async def notify(self, e: NotifyEvent) -> None:
            seen.append(e)

    clock = Clock()
    dispatcher = AlertDispatcher(Recorder(), run_id="r", cooldown_seconds=300, monotonic=clock)
    dispatcher.dispatch("emergency_stop", "nao e critico")
    dispatcher.dispatch("halted", "1")
    dispatcher.dispatch("halted", "2")
    dispatcher.dispatch("persistence_down", "3")
    clock.now = 301
    dispatcher.dispatch("halted", "4")
    await dispatcher.drain()
    assert [(e.kind, e.message) for e in seen] == [
        ("halted", "1"),
        ("persistence_down", "3"),
        ("halted", "4"),
    ]


async def test_dispatch_never_raises_or_blocks_on_failure_and_timeout() -> None:
    class Hang:
        async def notify(self, e: NotifyEvent) -> None:
            await asyncio.sleep(30)

    class Boom:
        async def notify(self, e: NotifyEvent) -> None:
            raise RuntimeError(f"erro com {TOKEN}")

    for notifier in (Hang(), Boom()):
        dispatcher = AlertDispatcher(notifier, cooldown_seconds=0, timeout_seconds=0.05)
        dispatcher.dispatch("halted", "x")
        await asyncio.wait_for(dispatcher.drain(), 2)


def test_every_notifiable_kind_has_severity_and_title() -> None:
    assert set(NOTIFY_KINDS) >= {
        "halted",
        "persistence_down",
        "stops_blind_prolonged",
        "daily_loss_pause",
    }
    assert all(sev in {"critical", "warning"} and title for sev, title in NOTIFY_KINDS.values())


def test_environment_enables_only_when_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTIFY_ENABLED", "true")
    monkeypatch.setenv("NOTIFY_TELEGRAM_CHAT_ID", "99")
    configured = NotifySettings(_env_file=None)  # type: ignore[call-arg]
    assert configured.enabled and configured.telegram_chat_id == "99"
