"""candles (hypertable) e audit_log

Revision ID: 0001
Revises:
Create Date: 2026-10-18
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PRICE = sa.Numeric(38, 18)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")

    op.create_table(
        "candles",
        sa.Column("symbol", sa.String(25), nullable=False),
        sa.Column("timeframe", sa.String(4), nullable=False),
        sa.Column("open_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", PRICE, nullable=False),
        sa.Column("high", PRICE, nullable=False),
        sa.Column("low", PRICE, nullable=False),
        sa.Column("close", PRICE, nullable=False),
        sa.Column("volume", PRICE, nullable=False),
        sa.PrimaryKeyConstraint("symbol", "timeframe", "open_time"),
        sa.CheckConstraint("high >= greatest(open, close) AND low <= least(open, close)", name="ck_candles_ohlc"),
        sa.CheckConstraint("volume >= 0", name="ck_candles_volume"),
    )
    op.execute(
        "SELECT create_hypertable('candles', 'open_time', chunk_time_interval => INTERVAL '30 days')"
    )

    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("actor", sa.String(100), nullable=False),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("entity", sa.String(100), nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    op.create_index("ix_audit_log_occurred_at", "audit_log", ["occurred_at"])


def downgrade() -> None:
    op.drop_table("audit_log")
    op.drop_table("candles")
