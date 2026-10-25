"""ai_calls: reserva atomica do teto mensal e resultado 'cancelled'

`reserve` e uma linha append-only com o custo de pior caso; a liquidacao e outra linha com `reservation_id`
apontando para ela (sem UPDATE). Sem linha de liquidacao a reserva segue contando como gasto.

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-24
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD = "result IN ('ok', 'invalid', 'timeout', 'budget', 'error')"
NEW = "result IN ('ok', 'invalid', 'timeout', 'budget', 'error', 'cancelled', 'reserve')"


def upgrade() -> None:
    op.add_column("ai_calls", sa.Column("reservation_id", sa.BigInteger))
    op.drop_constraint("ck_ai_calls_result", "ai_calls", type_="check")
    op.create_check_constraint("ck_ai_calls_result", "ai_calls", NEW)
    op.create_index(
        "ux_ai_calls_reservation",
        "ai_calls",
        ["reservation_id"],
        unique=True,
        postgresql_where=sa.text("reservation_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ux_ai_calls_reservation", table_name="ai_calls")
    op.drop_constraint("ck_ai_calls_result", "ai_calls", type_="check")
    op.create_check_constraint("ck_ai_calls_result", "ai_calls", OLD)
    op.drop_column("ai_calls", "reservation_id")
