"""Bancos descartaveis para testes destrutivos ou de migracao/papeis: nunca tocam o banco de desenvolvimento.

Todo teste que apaga dados, faz downgrade, troca papeis ou desliga triggers deve trabalhar num banco criado
aqui e passar a URL por `assert_disposable_database`, que recusa banco cujo nome nao termine em `_test`.
"""

import asyncio
import contextlib
import os
import secrets
import shutil
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import URL, make_url, text
from sqlalchemy.ext.asyncio import create_async_engine

from engine.infrastructure.config.settings import Settings

DISPOSABLE_SUFFIX = "_test"
ROLES_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "db_roles.sql"


class NotDisposableDatabaseError(RuntimeError):
    """Teste destrutivo apontado para um banco que nao e descartavel."""


def assert_disposable_database(target: URL | str) -> str:
    """Devolve o nome do banco se terminar em `_test`; senao levanta (e o teste destrutivo nem comeca)."""
    name = make_url(target).database
    if not name or not name.endswith(DISPOSABLE_SUFFIX):
        raise NotDisposableDatabaseError(
            f"recusado: o banco {name!r} nao termina em {DISPOSABLE_SUFFIX!r}; testes destrutivos so "
            "rodam em bancos descartaveis criados pela fixture"
        )
    return name


def _admin_url(settings: Settings) -> URL:
    return settings.database_url.set(database="postgres")


async def _execute_admin(settings: Settings, *statements: str) -> None:
    engine = create_async_engine(_admin_url(settings), isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            for statement in statements:
                await conn.execute(text(statement))
    finally:
        await engine.dispose()


def _psql_command(settings: Settings, database: str, env_names: list[str]) -> list[str]:
    base = ["-X", "-q", "-v", "ON_ERROR_STOP=1", "-U", settings.database_user, "-d", database, "-f", "-"]
    if shutil.which("psql"):
        return ["psql", "-h", settings.database_host, "-p", str(settings.database_port), *base]
    docker = shutil.which("docker")
    container = (
        subprocess.run(  # noqa: S603
            [docker, "ps", "-q", "--filter", f"publish={settings.database_port}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        ).stdout.split()
        if docker
        else []
    )
    if not container:
        raise RuntimeError(
            "psql indisponivel: instale o cliente ou suba o container do Timescale (scripts/dev.ps1 up)"
        )
    exec_env = [arg for name in env_names for arg in ("-e", name)]
    return [docker or "docker", "exec", "-i", *exec_env, container[0], "psql", *base]


def run_psql(settings: Settings, database: str, script: str, **secret_env: str) -> None:
    """Executa `script` no psql via stdin; segredos so viajam pelo ambiente (nunca em argv)."""
    env = {**os.environ, **secret_env, "PGPASSWORD": settings.database_password.get_secret_value()}
    command_line = _psql_command(settings, database, [*secret_env, "PGPASSWORD"])
    result = subprocess.run(  # noqa: S603
        command_line, input=script, capture_output=True, text=True, env=env, check=False, timeout=120
    )
    if result.returncode != 0:
        raise RuntimeError(f"psql falhou (codigo {result.returncode}): {result.stderr.strip()[:2000]}")


def upgrade_database(url: URL, revision: str = "head") -> None:
    assert_disposable_database(url)
    config = Config("alembic.ini")
    config.attributes["database_url"] = url
    command.upgrade(config, revision)


@dataclass(frozen=True, slots=True)
class RoleSetup:
    app_url: URL
    api_url: URL
    migrator_url: URL


@contextlib.contextmanager
def disposable_database(settings: Settings, label: str = "scratch") -> Iterator[URL]:
    """Cria `cryptoinvest_<label>_<hex>_test` (vazio) e o derruba ao sair."""
    name = f"cryptoinvest_{label}_{secrets.token_hex(4)}{DISPOSABLE_SUFFIX}"
    url = settings.database_url.set(database=name)
    assert_disposable_database(url)
    asyncio.run(_execute_admin(settings, f'CREATE DATABASE "{name}"'))
    try:
        yield url
    finally:
        asyncio.run(_execute_admin(settings, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


@contextlib.contextmanager
def restricted_roles(settings: Settings, url: URL) -> Iterator[RoleSetup]:
    """Aplica `scripts/db_roles.sql` no banco descartavel com papeis de nome unico (papeis sao do cluster).

    Ao sair derruba o proprio banco (os papeis donos de objetos nele nao poderiam ser removidos antes) e
    depois os papeis; o `disposable_database` externo apenas confirma que o banco ja nao existe.
    """
    database = assert_disposable_database(url)
    suffix = secrets.token_hex(4)
    app_role, api_role = f"app_{suffix}_test", f"api_{suffix}_test"
    migrator_role = f"migrator_{suffix}_test"
    app_password, api_password = secrets.token_urlsafe(18), secrets.token_urlsafe(18)
    migrator_password = secrets.token_urlsafe(18)
    preamble = f"\\set app_role {app_role}\n\\set api_role {api_role}\n\\set migrator_role {migrator_role}\n"
    try:
        run_psql(
            settings,
            database,
            preamble + ROLES_SCRIPT.read_text(encoding="utf-8"),
            CRYPTOINVEST_APP_PASSWORD=app_password,
            CRYPTOINVEST_API_PASSWORD=api_password,
            CRYPTOINVEST_MIGRATOR_PASSWORD=migrator_password,
        )
        yield RoleSetup(
            app_url=url.set(username=app_role, password=app_password),
            api_url=url.set(username=api_role, password=api_password),
            migrator_url=url.set(username=migrator_role, password=migrator_password),
        )
    finally:
        asyncio.run(_execute_admin(settings, f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)'))
        asyncio.run(
            _execute_admin(
                settings,
                f'DROP ROLE IF EXISTS "{app_role}"',
                f'DROP ROLE IF EXISTS "{api_role}"',
                f'DROP ROLE IF EXISTS "{migrator_role}"',
            )
        )
