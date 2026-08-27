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
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from atp_core.domain import StrategyState

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


#: The states a `strategies` row may hold, as a SQL predicate.
#:
#: Built from `StrategyState` rather than written out, so adding a rung to the
#: ratchet cannot leave the database accepting a value the domain does not know
#: — or refusing one it does.
STRATEGY_STATES_SQL = ", ".join(f"'{state.value}'" for state in StrategyState)


class StrategyRow(Base):
    __tablename__ = "strategies"
    #: `state` was a bare `String(20)` and the repository wrote `"active"` into
    #: it — a value `StrategyState` has never contained. Nothing anywhere
    #: objected, so every row in the table held a state no filter could match.
    #:
    #: A CHECK rather than a native PG enum: adding a rung is then one migration
    #: altering one constraint, instead of an `ALTER TYPE` that cannot run in a
    #: transaction on older servers. And unlike `audit_log.action` — which stays
    #: deliberately unconstrained because an append-only record must remain
    #: readable when the vocabulary changes — this column is current
    #: configuration over a closed set, so a value outside it is a bug rather
    #: than history.
    __table_args__ = (
        CheckConstraint(f"state IN ({STRATEGY_STATES_SQL})", name="ck_strategies_state"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    #: Either a registered class name + params, or an inline declarative RuleSet.
    kind: Mapped[str] = mapped_column(String(20))  # "coded" | "ruleset"
    class_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    params: Mapped[JsonDict] = mapped_column(JSON, default=dict)
    ruleset: Mapped[JsonDict | None] = mapped_column(JSON, nullable=True)
    #: The ratchet rung this strategy has been promoted to, never "is it running
    #: now" — that is `updated_at`. Defaulted from the enum so the default and
    #: the constraint above cannot disagree.
    state: Mapped[str] = mapped_column(String(20), default=StrategyState.DRAFT)
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
    __table_args__ = (
        Index("ix_signals_strategy_ts", "strategy_id", "ts"),
        # `/risk/rejections` reads the newest refusals, optionally for one
        # strategy and one rule. Neither existing index serves it: the one above
        # leads on `strategy_id`, and the query is not scoped to a strategy by
        # default — the first question is "is *anything* being refused".
        Index("ix_signals_rejected_by_ts", "rejected_by", "ts"),
    )

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
    #: Which rule refused this, or `no_action` for a HOLD-shaped outcome the
    #: router approved. Its own column as of `f4d2e8b1a075`.
    #:
    #: It was packed into `rejection_reason` as `"[rule] reason"` and split back
    #: out on read, and the repository said adding a column was "not worth a
    #: migration for one string". That was true while nothing queried it. It
    #: stopped being true the moment `/risk/rejections` needed to *filter* on
    #: the rule — excluding `no_action`, which is not a refusal — because that
    #: filter would otherwise be a `LIKE` against a bracketed prefix, matching
    #: any reason text that happened to start with one.
    rejected_by: Mapped[str | None] = mapped_column(String(50), nullable=True)


class OrderRow(Base):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("client_order_id", name="uq_orders_client_order_id"),
        Index("ix_orders_status_created", "status", "created_at"),
        Index("ix_orders_symbol_created", "symbol", "created_at"),
        # Trade reconstruction reads every filled order for a run mode in
        # created order, so this is the index that read walks. The existing two
        # lead on status and symbol and neither serves it: reconstruction is not
        # scoped to one symbol, and it wants both filled statuses rather than
        # one.
        Index("ix_orders_runmode_created", "run_mode", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    #: Unique constraint above is the database-level half of idempotency
    #: (rule §1.4) — a duplicate submit fails loudly instead of double-filling.
    client_order_id: Mapped[str] = mapped_column(String(128))
    broker_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    strategy_id: Mapped[str | None] = mapped_column(ForeignKey("strategies.id"), nullable=True)
    signal_id: Mapped[str | None] = mapped_column(ForeignKey("signals.id"), nullable=True)
    parent_order_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    #: Why this order exists — the `purpose` vocabulary in
    #: `atp_core.execution.idempotency`. Nullable because orders stored before
    #: the column existed genuinely do not know theirs, and a defaulted "entry"
    #: would label every historical exit as an entry. It is what makes an exit
    #: attributable: `analytics.performance` reads it to answer whether a
    #: strategy's profit comes from its targets while its stops bleed.
    purpose: Mapped[str | None] = mapped_column(String(20), nullable=True)

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
    #: *Who* refused this order, where `reject_reason` is *why* — a rule name
    #: (`max_gross_exposure`) or the pre-rule stage `routing` when `status` is
    #: `rejected_risk`, the broker's name when it is `rejected`. Added by
    #: `b8e3f01c7d24`.
    #:
    #: Nullable, and unlike `signals.rejected_by` it could not be backfilled:
    #: there the rule had been packed into the reason as `"[rule] reason"` and
    #: the migration parsed it back out, whereas an order's reason never
    #: carried it — `transition()` was given `decision.reason` and not
    #: `decision.rule`. Rows written before this column are null and stay null,
    #: which is why the screen distinguishes "nothing refused this" from
    #: "something refused it and did not say who".
    rejected_by: Mapped[str | None] = mapped_column(String(50), nullable=True)
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
    """One queued backtest, from the request to the result.

    Three timestamps, because a queue puts real time between being asked and
    starting (migration `d7a1c9f4b208`). `queued_at` is when somebody asked,
    `started_at` is when a worker picked it up — **null while it waits** — and
    `finished_at` is when it stopped either way. Stamping `started_at` at enqueue
    time, which is what this table's original shape forced, would make every
    run's reported duration include however long the queue was backed up.

    `metrics`, `equity_curve`, `trades`, `warnings` and `totals` are written
    together or not at all: a `done` row whose metrics landed and whose curve
    did not would claim a result it cannot show, one whose warnings did not land
    would claim a clean result it never had, and one whose totals did not land
    would report a return with no way to say how much of it was banked.
    """

    __tablename__ = "backtest_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    #: The registered strategy name, which is also its `strategies.id` — see
    #: `StrategyRecord`, where the same identity choice is explained. The foreign
    #: key means a backtest cannot name a strategy no worker has ever registered,
    #: which is a real constraint on this endpoint rather than a formality.
    strategy_id: Mapped[str] = mapped_column(ForeignKey("strategies.id"))
    #: The whole request, as `BacktestRunSpec` serialises it. Stored rather than
    #: normalised into columns because it is evidence: a result whose parameters
    #: nobody recorded cannot be compared with anything, and the set of
    #: parameters a backtest takes will grow.
    config: Mapped[JsonDict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20))  # queued|running|done|failed
    metrics: Mapped[JsonDict | None] = mapped_column(JSON, nullable=True)
    equity_curve: Mapped[JsonList | None] = mapped_column(JSON, nullable=True)
    #: The reconstructed round trips, from the same fold the live analytics use
    #: (`PerformanceAnalyzer.build_trades`). Stored rather than recomputed on
    #: read, unlike the live trades, and the difference is that those are folded
    #: from orders this database holds while a backtest's orders exist only
    #: inside the run that produced them.
    trades: Mapped[JsonList | None] = mapped_column(JSON, nullable=True)
    #: What the run said about itself: refusals, coverage shortfalls, the cost
    #: and sizing caveats `run_spec` attaches. Stored rather than recomputed
    #: because most of them are not recoverable from `metrics` — a run whose
    #: every order was refused has the same all-zero metric set as one that
    #: never signalled, and only this column tells them apart (migration
    #: `a9f37c14e6b2`).
    warnings: Mapped[JsonList | None] = mapped_column(JSON, nullable=True)
    #: What the run made and what it did — ending equity, the realised and
    #: unrealised split, fees, and the signal/order/fill counts. One column
    #: rather than nine, and separate from `metrics` rather than folded into it:
    #: `metrics` is float by contract and five of these are money, which must
    #: not be (CLAUDE.md §1.1). Decimal strings inside JSON, the way
    #: `equity_curve` already carries money (migration `f1b7c0d4e295`, ADR
    #: 0019).
    totals: Mapped[JsonDict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    #: Null until a worker claims it. A queued run has not started.
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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
