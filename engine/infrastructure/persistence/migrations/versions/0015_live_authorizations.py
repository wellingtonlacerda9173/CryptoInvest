"""autorizacoes do Live (append-only) e revogacoes (tabela filha append-only)

`live_authorizations`: registro imutavel de que, NO SERVIDOR, todos os criterios da trava valiam (criterios
avaliados em JSON, ator, motivo, frase, hash). Nao inicia nada. A revogacao e uma linha nova em
`live_authorization_revocations` (uma por autorizacao). A frase exata tambem e CHECK no banco.

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-24
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TZ = sa.DateTime(timezone=True)
TABLES = ("live_authorization_revocations", "live_authorizations")


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
        "live_authorizations",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("paper_run_id", sa.String(64), sa.ForeignKey("runs.run_id"), nullable=False),
        sa.Column("criteria", postgresql.JSONB, nullable=False),
        sa.Column("actor", sa.String(100), nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("confirmed_phrase", sa.String(64), nullable=False),
        sa.Column("initial_capital_quote", sa.Numeric(28, 10), nullable=False),
        sa.Column("credential_id", sa.String(36), nullable=False),
        sa.Column("key_permissions", postgresql.JSONB, nullable=False),
        sa.Column("record_hash", sa.String(64), nullable=False),
        sa.Column("created_at", TZ, nullable=False),
        sa.Column("expires_at", TZ, nullable=False),
        sa.Column("recorded_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "confirmed_phrase = 'OPERAR COM DINHEIRO REAL'", name="ck_live_authorizations_phrase"
        ),
        sa.CheckConstraint("expires_at > created_at", name="ck_live_authorizations_expiry"),
        sa.CheckConstraint("initial_capital_quote > 0", name="ck_live_authorizations_capital"),
        sa.CheckConstraint("length(btrim(reason)) >= 3", name="ck_live_authorizations_reason"),
    )
    op.create_index(
        "ix_live_authorizations_created", "live_authorizations", [sa.text("created_at DESC"), "id"]
    )
    op.create_table(
        "live_authorization_revocations",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column(
            "authorization_id", sa.String(32), sa.ForeignKey("live_authorizations.id"), nullable=False
        ),
        sa.Column("actor", sa.String(100), nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("revoked_at", TZ, nullable=False),
        sa.Column("recorded_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("length(btrim(reason)) >= 3", name="ck_live_authorization_revocations_reason"),
        sa.UniqueConstraint("authorization_id", name="uq_live_authorization_revocations_authorization"),
    )
    for table in TABLES[::-1]:
        _append_only(table)


def downgrade() -> None:
    for table in TABLES:
        op.execute(f"DROP TRIGGER {table}_no_truncate ON {table}")
        op.execute(f"DROP TRIGGER {table}_append_only ON {table}")
    op.drop_table("live_authorization_revocations")
    op.drop_table("live_authorizations")
