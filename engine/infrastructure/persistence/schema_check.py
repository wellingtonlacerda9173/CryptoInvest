"""Recusa subir com o schema do banco diferente do head do alembic que o codigo espera.

O papel da aplicacao nao le `alembic_version` (nenhum GRANT): a versao vem da funcao `public.schema_version()`
(SECURITY DEFINER, criada por `scripts/db_roles.sql` no job `migrate`).
"""

from pathlib import Path

from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


class SchemaMismatchError(RuntimeError):
    pass


def expected_head() -> str:
    head = ScriptDirectory(str(MIGRATIONS_DIR)).get_current_head()
    if head is None:
        raise SchemaMismatchError("o codigo nao tem migracoes do alembic")
    return head


async def database_version(engine: AsyncEngine) -> str | None:
    try:
        async with engine.connect() as connection:
            return (await connection.execute(text("SELECT public.schema_version()"))).scalar_one_or_none()
    except DBAPIError:
        return None


async def ensure_schema_current(engine: AsyncEngine) -> None:
    expected = expected_head()
    current = await database_version(engine)
    if current != expected:
        raise SchemaMismatchError(
            f"schema do banco ({current or 'indisponivel: rode o job migrate'}) diverge do esperado pelo "
            f"codigo ({expected}). Rode o job migrate (docs/deploy.md, secao 7) antes de subir; "
            "--skip-schema-check existe so para desenvolvimento."
        )
