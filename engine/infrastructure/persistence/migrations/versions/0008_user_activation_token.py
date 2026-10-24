"""token de ativacao de uso unico para o primeiro enrollment do 2FA e apos reset

- `users.activation_token_hash`: SHA-256 do token entregue ao admin uma unica vez (nunca o token).
- `users.activation_expires_at`: validade do token.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-20
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("activation_token_hash", sa.String(64), nullable=True))
    op.add_column("users", sa.Column("activation_expires_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "activation_expires_at")
    op.drop_column("users", "activation_token_hash")
