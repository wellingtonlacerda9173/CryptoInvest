"""Operacoes administrativas fora da API: bootstrap do primeiro admin, segredos e rotacao da chave mestra.

python -m api.cli init-secrets
python -m api.cli create-admin --username ana.silva
python -m api.cli new-master-key && python -m api.cli reencrypt
python -m api.cli unlock-user --username ana.silva
python -m api.cli check-grants   # falha se o papel da API perdeu UPDATE em colunas (reaplique db_roles.sql)
python -m api.cli issue-activation --username ana.silva   # novo token de ativacao do 2FA (conta sem 2FA)
"""

import argparse
import asyncio
import contextlib
import getpass
import os
import secrets
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from api.app import build_container
from api.audit import SYSTEM_META, AuditService
from api.config import ApiSettings, api_database_url, load_api_settings
from api.errors import ApiError, NotFound
from api.persistence.models import UserRow
from api.security.crypto import KeyRing, generate_key_line
from api.services.maintenance import reencrypt_all
from api.services.users import Activation, UserService
from engine.infrastructure.config.logging import configure_logging
from engine.infrastructure.config.settings import load_settings, resolve_secrets_dir
from engine.infrastructure.persistence.session import create_engine, create_session_factory

SESSION_SECRET_FILE = "api_session_secret"  # noqa: S105
MASTER_KEYS_FILE = "api_master_keys"
COMMAND_KEY_FILE = "command_hmac_key"
RECOVERY_KEY_FILE = "api_recovery_hmac_key"


def _actor() -> str:
    return f"cli:{getpass.getuser()}"[:100]


def _restrict_permissions(path: Path) -> None:
    if os.name == "posix":
        path.chmod(0o600)
        return
    # Windows: remove a heranca e deixa so o usuario atual (melhor esforco; falha nao impede o uso)
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(  # noqa: S603
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{getpass.getuser()}:(F)"],  # noqa: S607
            check=False,
            capture_output=True,
            timeout=15,
        )


def _write_secret(path: Path, content: str) -> None:
    """Cria o arquivo ja com permissao restrita e NUNCA sobrescreve (O_EXCL)."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)
    _restrict_permissions(path)


def init_secrets(secrets_dir: Path) -> list[str]:
    """Cria (sem sobrescrever) os segredos da API (dev; em producao use Docker secrets)."""
    created: list[str] = []
    secrets_dir.mkdir(parents=True, exist_ok=True)
    wanted: dict[str, Callable[[], str]] = {
        SESSION_SECRET_FILE: lambda: secrets.token_urlsafe(48),
        RECOVERY_KEY_FILE: lambda: secrets.token_urlsafe(48),
        MASTER_KEYS_FILE: lambda: generate_key_line(1) + "\n",
        # chave COMPARTILHADA com o runner (mesmo SECRETS_DIR): assina os comandos da API ao robo
        COMMAND_KEY_FILE: lambda: secrets.token_urlsafe(48),
    }
    for name, make in wanted.items():
        if not (secrets_dir / name).exists():
            _write_secret(secrets_dir / name, make())
            created.append(name)
    return created


def append_master_key(secrets_dir: Path) -> int:
    path = secrets_dir / MASTER_KEYS_FILE
    current = path.read_text(encoding="utf-8")
    ring = KeyRing.parse(current)
    version = ring.active_version + 1
    separator = "" if not current or current.endswith("\n") else "\n"
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(separator + generate_key_line(version) + "\n")
    return version


def _read_password(args: argparse.Namespace) -> str:
    if args.password_stdin:
        return sys.stdin.readline().rstrip("\r\n")
    first = getpass.getpass("Senha: ")
    if first != getpass.getpass("Repita a senha: "):
        raise SystemExit("As senhas nao conferem.")
    return first


async def _with_container(settings: ApiSettings):  # type: ignore[no-untyped-def]
    engine = create_engine(api_database_url(settings, load_settings().database_url))
    container = build_container(settings, create_session_factory(engine))
    return engine, container


def _print_activation(activation: Activation) -> None:
    print(
        "TOKEN DE ATIVACAO DO 2FA (mostrado UMA vez; repasse fora de banda, nao junto da senha): "
        f"{activation.token}\nValido ate {activation.expires_at:%Y-%m-%d %H:%M} UTC. "
        "Informe-o no primeiro login para configurar o 2FA."
    )


async def create_admin(args: argparse.Namespace, settings: ApiSettings) -> int:
    engine, container = await _with_container(settings)
    try:
        async with container.sessions() as db:
            existing = int(await db.scalar(select(func.count()).select_from(UserRow)) or 0)
        if existing and not args.allow_additional:
            print(
                "Ja existem usuarios. Use --allow-additional para criar outro administrador.", file=sys.stderr
            )
            return 1
        password = _read_password(args)
        created = await UserService(container, AuditService(container.sessions)).create(
            username=args.username, password=password, role="admin", actor=_actor(), meta=SYSTEM_META
        )
        print(f"Administrador '{created.user.username}' criado.")
        _print_activation(created.activation)
        return 0
    except ApiError as exc:
        print(f"Erro: {exc.detail or exc.title}", file=sys.stderr)
        return 1
    finally:
        await engine.dispose()


async def unlock_user(args: argparse.Namespace, settings: ApiSettings) -> int:
    """Passa pelo `UserService`: alem de zerar as colunas, incrementa `users.lock_epoch`; por essa epoca os
    processos da API em execucao soltam o bloqueio de senha que guardam em memoria."""
    engine, container = await _with_container(settings)
    try:
        await UserService(container, AuditService(container.sessions)).unlock_username(
            username=args.username, actor=_actor(), meta=SYSTEM_META, via="cli"
        )
        print("Usuario desbloqueado.")
        return 0
    except NotFound:
        print("Usuario nao encontrado.", file=sys.stderr)
        return 1
    finally:
        await engine.dispose()


# Colunas em que o papel da API PRECISA de UPDATE (espelha scripts/db_roles.sql); migracao que acrescenta
# coluna escrita pela API exige atualizar esta lista e reaplicar o script.
REQUIRED_API_UPDATES: dict[str, tuple[str, ...]] = {
    "users": (
        "password_hash",
        "role",
        "totp_secret",
        "totp_enabled",
        "totp_last_counter",
        "failed_attempts",
        "locked_until",
        "password_changed_at",
        "disabled",
        "activation_token_hash",
        "activation_expires_at",
        "lock_epoch",
    ),
    "control_commands": ("state", "channel", "delivered_at", "resolved_at", "result"),
    "backtest_jobs": (
        "status",
        "progress",
        "result",
        "error",
        "cancel_requested",
        "started_at",
        "finished_at",
    ),
}


async def missing_api_grants(session: AsyncSession) -> list[str]:
    missing: list[str] = []
    for table, columns in REQUIRED_API_UPDATES.items():
        for column in columns:
            allowed = await session.scalar(
                text("SELECT has_column_privilege(current_user, :t, :c, 'UPDATE')"), {"t": table, "c": column}
            )
            if not allowed:
                missing.append(f"{table}.{column}")
    return missing


async def check_grants(settings: ApiSettings) -> int:
    engine, container = await _with_container(settings)
    try:
        async with container.sessions() as db:
            missing = await missing_api_grants(db)
        if missing:
            print(
                "O papel da API nao tem UPDATE em: " + ", ".join(missing) + ". "
                "Reaplique scripts/db_roles.sql depois de cada migracao.",
                file=sys.stderr,
            )
            return 1
        print("Permissoes do papel da API conferem.")
        return 0
    finally:
        await engine.dispose()


async def issue_activation(args: argparse.Namespace, settings: ApiSettings) -> int:
    engine, container = await _with_container(settings)
    try:
        activation = await UserService(container, AuditService(container.sessions)).reissue_activation(
            username=args.username, actor=_actor(), meta=SYSTEM_META
        )
        _print_activation(activation)
        return 0
    except ApiError as exc:
        print(f"Erro: {exc.detail or exc.title}", file=sys.stderr)
        return 1
    finally:
        await engine.dispose()


async def reencrypt(settings: ApiSettings) -> int:
    engine, container = await _with_container(settings)
    try:
        report = await reencrypt_all(container, AuditService(container.sessions), actor=_actor())
        print(f"Recifrados: {report.totp_secrets} segredos TOTP, {report.credentials} credenciais.")
        if report.failed:
            print(
                "Nao foi possivel decifrar (versao de chave ausente ou dado corrompido): "
                + ", ".join(report.failed),
                file=sys.stderr,
            )
            return 1
        return 0
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m api.cli", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-secrets", help="gera segredos de dev que faltarem (nunca sobrescreve)")
    sub.add_parser("new-master-key", help="acrescenta nova versao da chave mestra (depois rode reencrypt)")
    sub.add_parser("reencrypt", help="recifra TOTP e credenciais com a chave mestra ativa")
    admin = sub.add_parser("create-admin", help="cria o primeiro administrador")
    admin.add_argument("--username", required=True)
    admin.add_argument("--password-stdin", action="store_true", help="le a senha da entrada padrao")
    admin.add_argument(
        "--allow-additional", action="store_true", help="permite criar admin mesmo com usuarios existentes"
    )
    unlock = sub.add_parser("unlock-user", help="zera o bloqueio por tentativas de um usuario")
    unlock.add_argument("--username", required=True)
    sub.add_parser("check-grants", help="falha se o papel do banco da API nao tem os UPDATE esperados")
    issue = sub.add_parser("issue-activation", help="emite novo token de ativacao do 2FA (conta sem 2FA)")
    issue.add_argument("--username", required=True)
    args = parser.parse_args(argv)

    secrets_dir = resolve_secrets_dir()
    if args.command in {"init-secrets", "new-master-key"}:
        target = secrets_dir or Path(os.environ.get("SECRETS_DIR", "./secrets"))
        if args.command == "init-secrets":
            created = init_secrets(target)
            print("Criados: " + ", ".join(created) if created else "Nada a criar: os segredos ja existem.")
        else:
            print(f"Chave mestra v{append_master_key(target)} adicionada. Rode `reencrypt` e reinicie a API.")
        return 0

    settings = load_api_settings()
    configure_logging(settings.log_level)
    if args.command == "create-admin":
        return asyncio.run(create_admin(args, settings))
    if args.command == "unlock-user":
        return asyncio.run(unlock_user(args, settings))
    if args.command == "check-grants":
        return asyncio.run(check_grants(settings))
    if args.command == "issue-activation":
        return asyncio.run(issue_activation(args, settings))
    return asyncio.run(reencrypt(settings))


if __name__ == "__main__":
    raise SystemExit(main())
