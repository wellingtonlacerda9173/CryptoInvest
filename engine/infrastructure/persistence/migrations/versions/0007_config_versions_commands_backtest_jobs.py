"""configuracao versionada (append-only), fila duravel de comandos e jobs de backtest da API

- `config_versions`: versoes imutaveis (trigger `forbid_modification` da 0004) com ator, motivo, diff e hash.
- `control_commands`: comandos do painel ao runner; estado visivel (`pending_delivery`, `delivered`, ...).
- `backtest_jobs`: backtests executados em subprocesso, com progresso e resultado.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-20
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "config_versions",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("parent_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("actor", sa.String(100), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("sections", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("activation", sa.String(12), nullable=False),
        sa.Column("risk_increase", sa.Boolean(), nullable=False),
        sa.Column("document", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("diff", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("chain_hash", sa.String(64), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("parent_id", name="uq_config_versions_parent"),
        sa.CheckConstraint("activation IN ('immediate', 'next_start')", name="ck_config_versions_activation"),
        sa.CheckConstraint("btrim(actor) <> '' AND btrim(reason) <> ''", name="ck_config_versions_identified"),
    )
    op.execute(
        "CREATE TRIGGER config_versions_append_only BEFORE UPDATE OR DELETE ON config_versions "
        "FOR EACH ROW EXECUTE FUNCTION forbid_modification()"
    )
    op.execute(
        "CREATE TRIGGER config_versions_no_truncate BEFORE TRUNCATE ON config_versions "
        "FOR EACH STATEMENT EXECUTE FUNCTION forbid_modification()"
    )
    op.create_table(
        "control_commands",
        sa.Column("id", sa.String(32), nullable=False),
        sa.Column("name", sa.String(20), nullable=False),
        sa.Column("operator", sa.String(100), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("liquidate", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "params",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("channel", sa.String(10), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "state IN ('pending_delivery', 'delivered', 'applied', 'discarded', 'expired', 'failed')",
            name="ck_control_commands_state",
        ),
    )
    op.create_index("ix_control_commands_state", "control_commands", ["state"])
    op.create_index("ix_control_commands_created_at", "control_commands", ["created_at"])
    op.create_table(
        "backtest_jobs",
        sa.Column("id", sa.String(32), nullable=False),
        sa.Column("created_by", sa.String(100), nullable=False),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("params", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "progress",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')", name="ck_backtest_jobs_status"
        ),
    )
    op.create_index("ix_backtest_jobs_created_at", "backtest_jobs", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_backtest_jobs_created_at", table_name="backtest_jobs")
    op.drop_table("backtest_jobs")
    op.drop_index("ix_control_commands_created_at", table_name="control_commands")
    op.drop_index("ix_control_commands_state", table_name="control_commands")
    op.drop_table("control_commands")
    op.execute("DROP TRIGGER config_versions_no_truncate ON config_versions")
    op.execute("DROP TRIGGER config_versions_append_only ON config_versions")
    op.drop_table("config_versions")
