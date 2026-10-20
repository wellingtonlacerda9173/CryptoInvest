"""ids de ordem por run, client_order_id de 64 caracteres e triggers append-only em fills/risk_state/audit_log/equity_snapshots

- `orders` passa a ser unica por (run_id, client_order_id) e `fills` referencia a ordem por essa chave
  composta: um run nunca enxerga nem colide com as ordens de outro.
- `client_order_id` cabe o sufixo ':oco' (36 + 4 caracteres) com folga.
- Os registros de execucao, estado de risco, auditoria e equity ficam imutaveis como trades/decisions.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-19
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPEND_ONLY_TABLES = ("fills", "risk_state", "audit_log", "equity_snapshots")


def upgrade() -> None:
    op.execute("ALTER TABLE fills DROP CONSTRAINT IF EXISTS fills_client_order_id_fkey")
    op.drop_constraint("uq_orders_client_order_id", "orders", type_="unique")
    op.alter_column("orders", "client_order_id", type_=sa.String(64), existing_nullable=False)
    op.alter_column("fills", "client_order_id", type_=sa.String(64), existing_nullable=False)
    op.alter_column("decisions", "client_order_id", type_=sa.String(64), existing_nullable=True)
    op.create_unique_constraint("uq_orders_run_client_order_id", "orders", ["run_id", "client_order_id"])
    op.create_foreign_key(
        "fk_fills_run_order",
        "fills",
        "orders",
        ["run_id", "client_order_id"],
        ["run_id", "client_order_id"],
    )
    op.drop_index("ix_fills_client_order_id", table_name="fills")
    op.create_index("ix_fills_client_order_id", "fills", ["run_id", "client_order_id"])

    for table in APPEND_ONLY_TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION forbid_modification()"
        )
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION forbid_modification()"
        )


def downgrade() -> None:
    for table in APPEND_ONLY_TABLES:
        op.execute(f"DROP TRIGGER {table}_no_truncate ON {table}")
        op.execute(f"DROP TRIGGER {table}_append_only ON {table}")

    op.drop_index("ix_fills_client_order_id", table_name="fills")
    op.create_index("ix_fills_client_order_id", "fills", ["client_order_id"])
    op.drop_constraint("fk_fills_run_order", "fills", type_="foreignkey")
    op.drop_constraint("uq_orders_run_client_order_id", "orders", type_="unique")
    # ids acima de 36 caracteres (OCO) nao cabem na coluna antiga: o downgrade falha em vez de truncar
    op.alter_column("decisions", "client_order_id", type_=sa.String(36), existing_nullable=True)
    op.alter_column("fills", "client_order_id", type_=sa.String(36), existing_nullable=False)
    op.alter_column("orders", "client_order_id", type_=sa.String(36), existing_nullable=False)
    op.create_unique_constraint("uq_orders_client_order_id", "orders", ["client_order_id"])
    op.create_foreign_key(
        "fills_client_order_id_fkey", "fills", "orders", ["client_order_id"], ["client_order_id"]
    )
