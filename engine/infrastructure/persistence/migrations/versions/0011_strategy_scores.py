"""snapshots de scores do seletor de estrategias (append-only)

Cada recalculo grava um snapshot novo (`snapshot_id`); nada e atualizado ou apagado, e o robo le o ultimo
snapshot por (timeframe, par). `origin` diz de onde veio a amostra (backtest validado, paper ou live).

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-24
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "strategy_scores"
TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("snapshot_id", sa.String(64), nullable=False),
        sa.Column("strategy", sa.String(100), nullable=False),
        sa.Column("regime", sa.String(30), nullable=False),
        sa.Column("symbol", sa.String(25), nullable=False),
        sa.Column("timeframe", sa.String(5), nullable=False),
        sa.Column("sample_size", sa.Integer, nullable=False),
        sa.Column("score", sa.Float, nullable=False),
        sa.Column("eligible", sa.Boolean, nullable=False),
        sa.Column("ineligible_reason", sa.Text),
        sa.Column("detail", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("calculated_at", TZ, nullable=False),
        sa.Column("origin", sa.String(10), nullable=False),
        sa.Column("run_id", sa.String(64)),
        sa.Column("recorded_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("origin IN ('backtest', 'paper', 'live')", name="ck_strategy_scores_origin"),
        sa.CheckConstraint("sample_size >= 0", name="ck_strategy_scores_sample"),
        sa.UniqueConstraint(
            "snapshot_id", "strategy", "regime", "symbol", "timeframe", name="uq_strategy_scores_snapshot_key"
        ),
    )
    op.create_index(
        "ix_strategy_scores_latest", TABLE, ["timeframe", "symbol", sa.text("calculated_at DESC")]
    )
    op.execute(
        f"CREATE TRIGGER {TABLE}_append_only BEFORE UPDATE OR DELETE ON {TABLE} "
        "FOR EACH ROW EXECUTE FUNCTION forbid_modification()"
    )
    op.execute(
        f"CREATE TRIGGER {TABLE}_no_truncate BEFORE TRUNCATE ON {TABLE} "
        "FOR EACH STATEMENT EXECUTE FUNCTION forbid_modification()"
    )


def downgrade() -> None:
    op.execute(f"DROP TRIGGER {TABLE}_no_truncate ON {TABLE}")
    op.execute(f"DROP TRIGGER {TABLE}_append_only ON {TABLE}")
    op.drop_table(TABLE)
