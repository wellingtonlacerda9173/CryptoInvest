"""diario de licoes da IA (append-only) e revisoes humanas (tabela filha append-only)

`ai_lessons`: o relatorio de licoes gerado pelo job, imutavel. A aprovacao NAO e coluna: cada decisao humana
e uma linha nova em `ai_lesson_reviews` (a mais recente vale; sem linha = pendente). Uma licao so entra no
prompt da IA com a revisao mais recente `approved` feita por admin.

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-24
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TZ = sa.DateTime(timezone=True)
TABLES = ("ai_lesson_reviews", "ai_lessons")


def _append_only(table: str) -> None:
    op.execute(
        f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
        "FOR EACH ROW EXECUTE FUNCTION forbid_modification()"
    )
    op.execute(
        f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
        "FOR EACH STATEMENT EXECUTE FUNCTION forbid_modification()"
    )


def upgrade() -> None:
    op.create_table(
        "ai_lessons",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("created_at", TZ, nullable=False),
        sa.Column("period_start", TZ, nullable=False),
        sa.Column("period_end", TZ, nullable=False),
        sa.Column("source", sa.String(14), nullable=False),
        sa.Column("model", sa.String(64)),
        sa.Column("prompt_version", sa.String(32), nullable=False),
        sa.Column("summary", postgresql.JSONB, nullable=False),
        sa.Column("report", postgresql.JSONB, nullable=False),
        sa.Column("recorded_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("source IN ('ai', 'deterministic')", name="ck_ai_lessons_source"),
        sa.CheckConstraint("period_end >= period_start", name="ck_ai_lessons_period"),
    )
    op.create_index("ix_ai_lessons_created", "ai_lessons", [sa.text("created_at DESC"), "id"])
    op.create_table(
        "ai_lesson_reviews",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("lesson_id", sa.String(32), sa.ForeignKey("ai_lessons.id"), nullable=False),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("reviewer", sa.String(64), nullable=False),
        sa.Column("reviewer_role", sa.String(10), nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("reviewed_at", TZ, nullable=False),
        sa.Column("recorded_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('pending', 'approved', 'rejected')", name="ck_ai_lesson_reviews_status"),
        sa.CheckConstraint("reviewer_role = 'admin'", name="ck_ai_lesson_reviews_admin"),
        sa.CheckConstraint("length(btrim(reason)) >= 3", name="ck_ai_lesson_reviews_reason"),
    )
    op.create_index("ix_ai_lesson_reviews_lesson", "ai_lesson_reviews", ["lesson_id", sa.text("id DESC")])
    for table in TABLES[::-1]:
        _append_only(table)


def downgrade() -> None:
    for table in TABLES:
        op.execute(f"DROP TRIGGER {table}_no_truncate ON {table}")
        op.execute(f"DROP TRIGGER {table}_append_only ON {table}")
    op.drop_table("ai_lesson_reviews")
    op.drop_table("ai_lessons")
