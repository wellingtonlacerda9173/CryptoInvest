"""precos de candles estritamente positivos

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-18
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_candles_positive_prices", "candles", "open > 0 AND high > 0 AND low > 0 AND close > 0"
    )


def downgrade() -> None:
    op.drop_constraint("ck_candles_positive_prices", "candles", type_="check")
