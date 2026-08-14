"""SQLAlchemy table definitions.

Schema notes that matter:

- **Money is NUMERIC, never DOUBLE PRECISION** (rule §1.1). `NUMERIC(20, 8)`
  handles equity prices, crypto, and fractional shares.
- **Every timestamp is TIMESTAMPTZ** (rule §1.2).
- **`bars` is a TimescaleDB hypertable** partitioned on `ts` — see
  `infra/db/init/01-timescale.sql`. It is the only unbounded table.
- **Orders and fills are append-mostly and never hard-deleted.** They are the
  audit trail; if a regulator or a post-mortem asks what happened, this is the
  answer. Cancel by status, not by DELETE.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

MONEY = Numeric(20, 8)


class Base(DeclarativeBase):
    pass


class BarRow(Base):
    """OHLCV. Hypertable — do NOT add a surrogate primary key; Timescale
    partitions on `ts` and the natural key is (symbol, timeframe, ts)."""

    __tablename__ = "bars"
    __table_args__ = (
        UniqueConstraint("symbol", "timeframe", "ts", name="uq_bars_symbol_tf_ts"),
        Index("ix_bars_symbol_tf_ts", "symbol", "timeframe", "ts"),
    )

    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    timeframe: Mapped[str] = mapped_column(String(8), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    open: Mapped[Decimal] = mapped_column(MONEY)
    high: Mapped[Decimal] = mapped_column(MONEY)
    low: Mapped[Decimal] = mapped_column(MONEY)
    close: Mapped[Decimal] = mapped_column(MONEY)
    volume: Mapped[Decimal] = mapped_column(MONEY)
    adj_close: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    vwap: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    trade_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class StrategyRow(Base):
    __tablename__ = "strategies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    #: Either a registered class name + params, or an inline declarative RuleSet.
    kind: Mapped[str] = mapped_column(String(20))  # "coded" | "ruleset"
    class_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    ruleset: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    state: Mapped[str] = mapped_column(String(20), default="draft")
    universe: Mapped[list] = mapped_column(JSON, default=list)
    timeframe: Mapped[str] = mapped_column(String(8), default="1d")
    risk_config: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    orders: Mapped[list[OrderRow]] = relationship(back_populates="strategy")


class SignalRow(Base):
    """Why a trade happened. Keep even when no order resulted — a signal the
    risk engine blocked is exactly what you want to see when asking why a
    strategy underperformed its backtest."""

    __tablename__ = "signals"
    __table_args__ = (Index("ix_signals_strategy_ts", "strategy_id", "ts"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    strategy_id: Mapped[str] = mapped_column(ForeignKey("strategies.id"))
    symbol: Mapped[str] = mapped_column(String(20))
    action: Mapped[str] = mapped_column(String(20))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    strength: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(1))
    reason: Mapped[str] = mapped_column(Text, default="")
    indicators: Mapped[dict] = mapped_column(JSON, default=dict)
    acted_on: Mapped[bool] = mapped_column(Boolean, default=False)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class OrderRow(Base):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("client_order_id", name="uq_orders_client_order_id"),
        Index("ix_orders_status_created", "status", "created_at"),
        Index("ix_orders_symbol_created", "symbol", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    #: Unique constraint above is the database-level half of idempotency
    #: (rule §1.4) — a duplicate submit fails loudly instead of double-filling.
    client_order_id: Mapped[str] = mapped_column(String(128))
    broker_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    strategy_id: Mapped[str | None] = mapped_column(ForeignKey("strategies.id"), nullable=True)
    signal_id: Mapped[str | None] = mapped_column(ForeignKey("signals.id"), nullable=True)
    parent_order_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    symbol: Mapped[str] = mapped_column(String(20))
    side: Mapped[str] = mapped_column(String(8))
    order_type: Mapped[str] = mapped_column(String(20))
    time_in_force: Mapped[str] = mapped_column(String(8))
    qty: Mapped[Decimal] = mapped_column(MONEY)
    limit_price: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    stop_price: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)

    status: Mapped[str] = mapped_column(String(24))
    filled_qty: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    avg_fill_price: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    reject_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    run_mode: Mapped[str] = mapped_column(String(10))  # paper vs live must be separable

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    filled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    strategy: Mapped[StrategyRow | None] = relationship(back_populates="orders")
    fills: Mapped[list[FillRow]] = relationship(back_populates="order")


class FillRow(Base):
    __tablename__ = "fills"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"))
    venue_fill_id: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    qty: Mapped[Decimal] = mapped_column(MONEY)
    price: Mapped[Decimal] = mapped_column(MONEY)
    fee: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))

    order: Mapped[OrderRow] = relationship(back_populates="fills")


class PositionSnapshotRow(Base):
    """Periodic position snapshots — what the dashboard's history charts read,
    and what reconciliation compares against after a restart."""

    __tablename__ = "position_snapshots"
    __table_args__ = (Index("ix_possnap_ts_symbol", "ts", "symbol"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    symbol: Mapped[str] = mapped_column(String(20))
    qty: Mapped[Decimal] = mapped_column(MONEY)
    avg_entry_price: Mapped[Decimal] = mapped_column(MONEY)
    last_price: Mapped[Decimal] = mapped_column(MONEY)
    unrealized_pnl: Mapped[Decimal] = mapped_column(MONEY)
    realized_pnl: Mapped[Decimal] = mapped_column(MONEY)
    stop_loss_price: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    run_mode: Mapped[str] = mapped_column(String(10))


class EquitySnapshotRow(Base):
    __tablename__ = "equity_snapshots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    equity: Mapped[Decimal] = mapped_column(MONEY)
    cash: Mapped[Decimal] = mapped_column(MONEY)
    gross_exposure: Mapped[Decimal] = mapped_column(MONEY)
    run_mode: Mapped[str] = mapped_column(String(10))


class BacktestRunRow(Base):
    __tablename__ = "backtest_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    strategy_id: Mapped[str] = mapped_column(ForeignKey("strategies.id"))
    config: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20))  # queued|running|done|failed
    metrics: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    equity_curve: Mapped[list | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditLogRow(Base):
    """Every consequential human action: kill switch, live-mode toggle, manual
    order, strategy promotion to live. Append-only, never edited."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    actor: Mapped[str] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(50))
    target: Mapped[str | None] = mapped_column(String(100), nullable=True)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
