"""diario do robo: runs, ordens, fills, posicoes, trades, decisoes, estados e controle

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-19
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PRICE = sa.Numeric(38, 18)
TZ = sa.DateTime(timezone=True)


def _id() -> sa.Column[int]:
    return sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True)


def _run_fk() -> sa.Column[str]:
    return sa.Column("run_id", sa.String(64), sa.ForeignKey("runs.run_id"), nullable=False)


def upgrade() -> None:
    op.create_table(
        "runs",
        sa.Column("run_id", sa.String(64), primary_key=True),
        sa.Column("mode", sa.String(10), nullable=False),
        sa.Column("started_at", TZ, nullable=False),
        sa.Column("ended_at", TZ),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("code_version", sa.String(64), nullable=False),
        sa.CheckConstraint("mode IN ('paper', 'live', 'backtest')", name="ck_runs_mode"),
    )

    op.create_table(
        "orders",
        _id(),
        sa.Column("client_order_id", sa.String(36), nullable=False),
        _run_fk(),
        sa.Column("symbol", sa.String(25), nullable=False),
        sa.Column("side", sa.String(4), nullable=False),
        sa.Column("type", sa.String(10), nullable=False),
        sa.Column("quantity", PRICE, nullable=False),
        sa.Column("price", PRICE),
        sa.Column("stop_loss", PRICE),
        sa.Column("take_profit", PRICE),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("exchange_order_id", sa.String(64)),
        sa.Column("filled_quantity", PRICE, nullable=False, server_default=sa.text("0")),
        sa.Column("average_price", PRICE),
        sa.Column("created_at", TZ, nullable=False),
        sa.Column("updated_at", TZ, nullable=False),
        sa.UniqueConstraint("client_order_id", name="uq_orders_client_order_id"),
        sa.CheckConstraint("side IN ('buy', 'sell')", name="ck_orders_side"),
        sa.CheckConstraint("quantity > 0", name="ck_orders_quantity"),
    )
    op.create_index("ix_orders_run_status", "orders", ["run_id", "status"])

    op.create_table(
        "fills",
        _id(),
        sa.Column("fill_id", sa.String(64), nullable=False),
        _run_fk(),
        sa.Column("client_order_id", sa.String(36), sa.ForeignKey("orders.client_order_id"), nullable=False),
        sa.Column("symbol", sa.String(25), nullable=False),
        sa.Column("side", sa.String(4), nullable=False),
        sa.Column("quantity", PRICE, nullable=False),
        sa.Column("price", PRICE, nullable=False),
        sa.Column("fee", PRICE, nullable=False),
        sa.Column("fee_asset", sa.String(12)),
        sa.Column("filled_at", TZ, nullable=False),
        sa.UniqueConstraint("run_id", "fill_id", name="uq_fills_run_fill_id"),
        sa.CheckConstraint("side IN ('buy', 'sell')", name="ck_fills_side"),
        sa.CheckConstraint("quantity > 0 AND price > 0 AND fee >= 0", name="ck_fills_positive"),
    )
    op.create_index("ix_fills_run_filled_at", "fills", ["run_id", "filled_at"])
    op.create_index("ix_fills_client_order_id", "fills", ["client_order_id"])

    op.create_table(
        "positions",
        _id(),
        _run_fk(),
        sa.Column("symbol", sa.String(25), nullable=False),
        sa.Column("entry_price", PRICE, nullable=False),
        sa.Column("initial_quantity", PRICE, nullable=False),
        sa.Column("quantity", PRICE, nullable=False),
        sa.Column("initial_stop", PRICE, nullable=False),
        sa.Column("stop", PRICE, nullable=False),
        sa.Column("take_profit", PRICE),
        sa.Column("highest_price", PRICE, nullable=False),
        sa.Column("entry_fee", PRICE, nullable=False),
        sa.Column("breakeven_reached", sa.Boolean, nullable=False),
        sa.Column("partial_done", sa.Boolean, nullable=False),
        sa.Column("realized_pnl", PRICE, nullable=False),
        sa.Column("opened_at", TZ, nullable=False),
        sa.Column("closed_at", TZ),
        sa.CheckConstraint("quantity > 0 AND initial_quantity >= quantity", name="ck_positions_quantity"),
        sa.CheckConstraint("stop >= initial_stop AND initial_stop < entry_price", name="ck_positions_stops"),
    )
    op.create_index(
        "uq_positions_open_symbol",
        "positions",
        ["run_id", "symbol"],
        unique=True,
        postgresql_where=sa.text("closed_at IS NULL"),
    )

    op.create_table(
        "trades",
        _id(),
        sa.Column("trade_id", sa.String(64), nullable=False),
        _run_fk(),
        sa.Column("symbol", sa.String(25), nullable=False),
        sa.Column("quantity", PRICE, nullable=False),
        sa.Column("entry_price", PRICE, nullable=False),
        sa.Column("exit_price", PRICE, nullable=False),
        sa.Column("entry_fee", PRICE, nullable=False),
        sa.Column("exit_fee", PRICE, nullable=False),
        sa.Column("pnl", PRICE, nullable=False),
        sa.Column("opened_at", TZ, nullable=False),
        sa.Column("closed_at", TZ, nullable=False),
        sa.Column("exit_reason", sa.String(20), nullable=False),
        sa.Column("strategy", sa.String(100)),
        sa.Column("risk_amount", PRICE),
        sa.Column("r_multiple", PRICE),
        sa.Column("recorded_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("run_id", "trade_id", name="uq_trades_run_trade_id"),
        sa.CheckConstraint("closed_at >= opened_at", name="ck_trades_times"),
        sa.CheckConstraint(
            "exit_reason IN ('stop_loss', 'take_profit', 'profit_protection', 'signal', 'manual')",
            name="ck_trades_exit_reason",
        ),
    )
    op.create_index("ix_trades_run_closed_at", "trades", ["run_id", "closed_at"])

    op.create_table(
        "decisions",
        _id(),
        sa.Column("decision_key", sa.String(48), nullable=False),
        _run_fk(),
        sa.Column("decided_at", TZ, nullable=False),
        sa.Column("symbol", sa.String(25), nullable=False),
        sa.Column("action", sa.String(10), nullable=False),
        sa.Column("traded", sa.Boolean, nullable=False),
        sa.Column("code", sa.String(50), nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("strategy", sa.String(100)),
        sa.Column("confidence", sa.Float),
        sa.Column("signal_id", sa.String(64)),
        sa.Column("client_order_id", sa.String(36)),
        sa.Column("signal", postgresql.JSONB),
        sa.Column("context", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("recorded_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("run_id", "decision_key", name="uq_decisions_run_decision_key"),
    )
    op.create_index("ix_decisions_run_decided_at", "decisions", ["run_id", "decided_at"])
    op.create_index("ix_decisions_signal_id", "decisions", ["signal_id"])

    for table in ("risk_state", "gateway_state"):
        op.create_table(
            table,
            _id(),
            _run_fk(),
            sa.Column("version", sa.Integer, nullable=False),
            sa.Column("saved_at", TZ, nullable=False),
            sa.Column("state", postgresql.JSONB, nullable=False),
            sa.UniqueConstraint("run_id", "version", name=f"uq_{table}_run_version"),
        )

    op.create_table(
        "equity_snapshots",
        sa.Column("run_id", sa.String(64), sa.ForeignKey("runs.run_id"), primary_key=True),
        sa.Column("at", TZ, primary_key=True),
        sa.Column("equity", PRICE, nullable=False),
        sa.Column("realized", PRICE, nullable=False),
        sa.Column("in_position", sa.Boolean, nullable=False),
    )
    op.execute("SELECT create_hypertable('equity_snapshots', 'at', chunk_time_interval => INTERVAL '30 days')")

    op.create_table(
        "control_events",
        _id(),
        _run_fk(),
        sa.Column("at", TZ, nullable=False),
        sa.Column("action", sa.String(12), nullable=False),
        sa.Column("operator", sa.String(100), nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("recorded_at", TZ, server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "action IN ('start', 'stop', 'pause', 'resume', 'reactivate', 'halt')",
            name="ck_control_events_action",
        ),
        sa.CheckConstraint("btrim(operator) <> '' AND btrim(reason) <> ''", name="ck_control_events_identified"),
    )
    op.create_index("ix_control_events_run_at", "control_events", ["run_id", "at"])


def downgrade() -> None:
    for table in (
        "control_events",
        "equity_snapshots",
        "gateway_state",
        "risk_state",
        "decisions",
        "trades",
        "positions",
        "fills",
        "orders",
        "runs",
    ):
        op.drop_table(table)
