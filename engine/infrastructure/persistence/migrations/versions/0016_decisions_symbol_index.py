"""indice (symbol, decided_at) em decisions para as decisoes do grafico por par

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-25
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX = "ix_decisions_symbol_decided_at"


def upgrade() -> None:
    op.create_index(INDEX, "decisions", ["symbol", "decided_at"])


def downgrade() -> None:
    op.drop_index(INDEX, table_name="decisions")
