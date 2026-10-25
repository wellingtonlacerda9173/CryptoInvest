"""registro append-only das chamadas ao analista de IA (custo, tokens, resultado, hashes)

Uma linha por chamada, inclusive falhas e recusas por orcamento. Nada e atualizado ou apagado. Sem dados
sensiveis: so hashes do prompt/resposta, o texto sanitizado da resposta, tokens, custo estimado e latencia.

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-24
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "ai_calls"
TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("called_at", TZ, nullable=False),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("prompt_version", sa.String(32), nullable=False),
        sa.Column("prompt_hash", sa.String(64), nullable=False),
        sa.Column("response_hash", sa.String(64)),
        sa.Column("response_text", sa.Text),
        sa.Column("input_tokens", sa.Integer, nullable=False),
        sa.Column("output_tokens", sa.Integer, nullable=False),
        sa.Column("cost_usd", sa.Numeric(14, 6), nullable=False),
        sa.Column("latency_ms", sa.Integer, nullable=False),
        sa.Column("result", sa.String(10), nullable=False),
        sa.Column("decision_key", sa.String(64)),
        sa.Column("recorded_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("result IN ('ok', 'invalid', 'timeout', 'budget', 'error')", name="ck_ai_calls_result"),
        sa.CheckConstraint("input_tokens >= 0 AND output_tokens >= 0 AND cost_usd >= 0", name="ck_ai_calls_usage"),
    )
    op.create_index("ix_ai_calls_called_at", TABLE, [sa.text("called_at DESC")])
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
