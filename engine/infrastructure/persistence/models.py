from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Identity, Index, Numeric, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

PRICE = Numeric(38, 18)


class Base(DeclarativeBase):
    pass


class CandleRow(Base):
    """Hypertable Timescale particionada por open_time. Somente velas fechadas."""

    __tablename__ = "candles"
    __table_args__ = (
        CheckConstraint(
            "high >= greatest(open, close) AND low <= least(open, close)", name="ck_candles_ohlc"
        ),
        CheckConstraint("volume >= 0", name="ck_candles_volume"),
        CheckConstraint("open > 0 AND high > 0 AND low > 0 AND close > 0", name="ck_candles_positive_prices"),
        # criado automaticamente pelo create_hypertable da migracao 0001
        Index("candles_open_time_idx", text("open_time DESC")),
    )

    symbol: Mapped[str] = mapped_column(String(25), primary_key=True)
    timeframe: Mapped[str] = mapped_column(String(4), primary_key=True)
    open_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    open: Mapped[Decimal] = mapped_column(PRICE)
    high: Mapped[Decimal] = mapped_column(PRICE)
    low: Mapped[Decimal] = mapped_column(PRICE)
    close: Mapped[Decimal] = mapped_column(PRICE)
    volume: Mapped[Decimal] = mapped_column(PRICE)


class AuditLogRow(Base):
    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_log_occurred_at", "occurred_at"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    actor: Mapped[str] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(100))
    entity: Mapped[str] = mapped_column(String(100))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
