"""epoca de desbloqueio por usuario (desbloqueio efetivo entre processos)

- `users.lock_epoch`: incrementada a cada desbloqueio administrativo (API ou CLI). Cada processo da API
  guarda o bloqueio de senha em memoria e compara a epoca para soltar o que outro processo ja liberou.

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-22
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("lock_epoch", sa.Integer(), server_default=sa.text("0"), nullable=False))


def downgrade() -> None:
    op.drop_column("users", "lock_epoch")
