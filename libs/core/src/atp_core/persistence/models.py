"""SQLAlchemy table definitions.

Schema notes that matter:

- **Money is NUMERIC, never DOUBLE PRECISION** (rule §1.1). `NUMERIC(20, 8)`
  handles equity prices, crypto, and fractional shares.
- **Every timestamp is TIMESTAMPTZ** (rule §1.2).
- **`bars` is a TimescaleDB hypertable** partitioned on `ts`, converted by the
  initial migration. It is the only unbounded table.
- **Orders and fills are append-mostly and never hard-deleted.** They are the
  audit trail; if a regulator or a post-mortem asks what happened, this is the
  answer. Cancel by status, not by DELETE.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

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

#: JSON columns hold arbitrary decoded JSON. Naming the shape beats a bare
#: `dict`, which mypy --strict rejects as an untyped generic.
type JsonDict = dict[str, Any]
type JsonList = list[Any]


class Base(DeclarativeBase):
    pass


class BarRow(Base):
    """OHLCV. Hypertable — do NOT add a surrogate primary key; Timescale
    partitions on `ts` and the natural key is (symbol, timeframe, ts)."""

    __tablename__ = "bars"
    # The composite primary key below IS the natural key, so this table
    # deliberately declares nothing else on those columns. It previously carried
    # a matching UniqueConstraint and Index as well, which cost more than the
    # duplication suggests: the index is a second btree maintained on every
    # chunk of the one table that grows without bound, and the constraint was
    # never a separate object at all — SQLAlchemy folds it into the primary key,
    # so autogenerate reflected a plain PK, failed to find the unique constraint
    # it expected, and proposed adding it on every single `make revision`.
    # Upserts infer the arbiter from the columns — `ON CONFLICT (symbol,
    # timeframe, ts)` — so nothing needs the constraint name.

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
    params: Mapped[JsonDict] = mapped_column(JSON, default=dict)
    ruleset: Mapped[JsonDict | None] = mapped_column(JSON, nullable=True)
    state: Mapped[str] = mapped_column(String(20), default="draft")
    universe: Mapped[JsonList] = mapped_column(JSON, default=list)
    timeframe: Mapped[str] = mapped_column(String(8), default="1d")
    risk_config: Mapped[JsonDict] = mapped_column(JSON, default=dict)
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
    indicators: Mapped[JsonDict] = mapped_column(JSON, default=dict)
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
    #: The other two protective fields a `Position` carries. Added because a
    #: snapshot that restores a position without them is worse than one that
    #: restores nothing: a trailing stop reloaded with no high-water mark
    #: re-anchors on the current bar, and `update_trailing`'s monotonicity
    #: invariant then holds around a mark that has moved *down*. A take-profit
    #: silently dropped is a position with no upside exit.
    take_profit_price: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    high_water_mark: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    #: Nullable because a position adopted from the broker has no opening time
    #: we know of, and a time stop measuring from "now" would exit late rather
    #: than never — worth telling apart from a genuine zero.
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fees_paid: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
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
    config: Mapped[JsonDict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20))  # queued|running|done|failed
    metrics: Mapped[JsonDict | None] = mapped_column(JSON, nullable=True)
    equity_curve: Mapped[JsonList | None] = mapped_column(JSON, nullable=True)
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
    detail: Mapped[JsonDict] = mapped_column(JSON, default=dict)
