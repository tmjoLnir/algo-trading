"""In-memory test doubles.

`FakeBroker` is the only broker the test suite may talk to (CLAUDE.md §1.7). It
is deliberately not `SimulatedBroker`: that one is a fill *simulator* whose
realism bounds every backtest, and it is a Phase 4 item of its own. This is a
controllable stand-in for a venue, built to be told to fail.

The failures it can be told to produce are the ones that matter:

- a partial fill, so a position update meets a fill *sequence* (CLAUDE.md §5);
- a venue rejection, which is an ordinary outcome and not an exception path;
- a submit that times out **after** the venue accepted the order, which is the
  case that makes a blind resubmit create a second position;
- a submit that times out having never reached the venue;
- a broker that is unreachable for reads as well as writes.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from atp_core.brokers.ports import AccountSnapshot
from atp_core.domain import Fill, Order, OrderStatus, OrderType, Portfolio, Position
from atp_core.errors import BrokerConnectionError, OrderRejectedError
from atp_core.execution.ports import StoredBook

if TYPE_CHECKING:
    from typing import Any

    from atp_core.backtest.ports import BacktestProgress, StoredBacktestRun
    from atp_core.dashboard.snapshot import LiveSnapshot
    from atp_core.domain import Side, Signal
    from atp_core.execution.ports import EquityPoint
    from atp_core.strategy.ports import SignalOutcome, StoredStrategy, StrategyRecord


class FakeBroker:
    """A `BrokerPort` you can make misbehave on demand."""

    def __init__(self, *, equity: Decimal = Decimal("100000")) -> None:
        self.equity = equity
        #: client_order_id → the venue's copy of the order. A *copy*: an adapter
        #: that handed back the caller's own object would make adoption look
        #: like it worked when nothing was adopted.
        self.accepted: dict[str, Order] = {}
        self.positions: dict[str, Position] = {}
        self.cancelled: list[str] = []
        self.submit_calls: list[str] = []
        self.market_open = True

        # ── the levers ──────────────────────────────────────────────────────
        #: Reject the next submit with this reason (a venue refusal).
        self.reject_next: str | None = None
        #: Time out the next submit. `accept_on_timeout` decides whether the
        #: venue got it anyway — the difference between a lost order and a
        #: hidden one, and the whole reason not to resubmit blind.
        self.timeout_next = False
        self.accept_on_timeout = False
        #: Reads fail too — an adapter cannot look up what it cannot reach.
        self.reads_fail = False

        self._next_id = 0

    # ── BrokerPort ──────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "fake"

    @property
    def supports_fractional(self) -> bool:
        return True

    async def get_account(self) -> AccountSnapshot:
        self._guard_reads()
        return AccountSnapshot(
            account_id="fake-account",
            equity=self.equity,
            cash=self.equity,
            buying_power=self.equity,
            maintenance_margin=Decimal(0),
            is_pattern_day_trader=False,
            trading_blocked=False,
            as_of=datetime(2024, 6, 3, 14, 30, tzinfo=UTC),
        )

    async def submit_order(self, order: Order) -> Order:
        self.submit_calls.append(order.client_order_id)

        if self.reject_next is not None:
            reason, self.reject_next = self.reject_next, None
            raise OrderRejectedError(reason)

        if self.timeout_next:
            self.timeout_next = False
            if self.accept_on_timeout:
                self._accept(order)
            raise BrokerConnectionError("connection reset by peer")

        # Idempotent on client_order_id, as the port requires: a resubmit of a
        # key the venue already holds returns what it holds rather than opening
        # a second order.
        if order.client_order_id in self.accepted:
            return self.accepted[order.client_order_id]
        return self._accept(order)

    async def cancel_order(self, broker_order_id: str) -> None:
        self._guard_reads()
        self.cancelled.append(broker_order_id)
        for held in self.accepted.values():
            if held.broker_order_id == broker_order_id and not held.is_complete:
                held.status = OrderStatus.CANCELLED

    async def get_order(self, broker_order_id: str) -> Order | None:
        self._guard_reads()
        return next(
            (o for o in self.accepted.values() if o.broker_order_id == broker_order_id), None
        )

    async def get_open_orders(self) -> list[Order]:
        self._guard_reads()
        return [o for o in self.accepted.values() if not o.is_complete]

    async def get_positions(self) -> list[Position]:
        self._guard_reads()
        return [p for p in self.positions.values() if not p.is_flat]

    async def close_position(self, symbol: str) -> Order:
        raise NotImplementedError("the router flattens through submit(), never through here")

    async def close_all_positions(self) -> list[Order]:
        raise NotImplementedError("emergency flatten is a runbook path, not a router path")

    async def is_market_open(self) -> bool:
        self._guard_reads()
        return self.market_open

    # ── the levers, operated ────────────────────────────────────────────────

    def fill(
        self,
        client_order_id: str,
        qty: Decimal,
        price: Decimal,
        *,
        fee: Decimal = Decimal(0),
        at: datetime | None = None,
    ) -> Fill:
        """Fill part or all of an accepted order, venue-side."""
        held = self.accepted[client_order_id]
        fill = Fill(
            order_id=held.id,
            ts=at or datetime(2024, 6, 3, 14, 31, tzinfo=UTC),
            qty=qty,
            price=price,
            fee=fee,
        )
        held.apply_fill(fill)
        return fill

    def hold(self, symbol: str, qty: Decimal, avg_entry_price: Decimal) -> None:
        """Give the venue a position, for reconciliation-shaped assertions."""
        self.positions[symbol] = Position(
            symbol=symbol, qty=qty, avg_entry_price=avg_entry_price, last_price=avg_entry_price
        )

    def order_for(self, client_order_id: str) -> Order:
        return self.accepted[client_order_id]

    def open_order_count(
        self,
        symbol: str | None = None,
        side: Side | None = None,
        order_type: OrderType | None = None,
    ) -> int:
        return sum(
            1
            for o in self.accepted.values()
            if not o.is_complete
            and (symbol is None or o.symbol == symbol)
            and (side is None or o.side is side)
            and (order_type is None or o.order_type is order_type)
        )

    def open_stops(self, symbol: str | None = None, side: Side | None = None) -> int:
        """Protective orders only — an entry resting at the venue is not one."""
        return self.open_order_count(symbol, side, OrderType.STOP)

    # ── internals ───────────────────────────────────────────────────────────

    def _accept(self, order: Order) -> Order:
        self._next_id += 1
        held = replace(
            order,
            broker_order_id=f"brk-{self._next_id}",
            status=OrderStatus.SUBMITTED,
            fills=[],
        )
        self.accepted[order.client_order_id] = held
        return held

    def _guard_reads(self) -> None:
        if self.reads_fail:
            raise BrokerConnectionError("broker unreachable")


class FakeKillSwitch:
    """Records halts instead of reaching Redis."""

    def __init__(self, engaged: bool = False) -> None:
        self.engaged = engaged
        self.engagements: list[tuple[str, str, str, str]] = []
        #: What `active_halts` reports. Seeded by a test that needs the halt
        #: banner to have something to render; the engagement list above is a
        #: record of calls rather than of state, and the two are separate
        #: because a test usually cares about exactly one of them.
        self.halts: list[object] = []

    def is_engaged(self, strategy_id: str | None = None, symbol: str | None = None) -> bool:
        return self.engaged

    def engage(
        self,
        scope: object,
        reason: object,
        engaged_by: str,
        detail: str = "",
        target: str | None = None,
    ) -> object:
        self.engaged = True
        self.engagements.append((str(scope), str(reason), engaged_by, detail))
        return None

    def clear(self, scope: object, cleared_by: str, target: str | None = None) -> None:
        self.engaged = False

    def active_halts(self) -> list[object]:
        return list(self.halts)


class FakeOrderRepository:
    """In-memory `OrderRepository`, keyed the way the real one is.

    Stores a *copy* of each order. The runner mutates the orders it holds, and
    a fake that kept the same object would make "was it saved?" unanswerable —
    every assertion would see the live object rather than what was written.
    """

    def __init__(self) -> None:
        self.saved: dict[str, Order] = {}
        self.save_calls: list[str] = []
        #: Seeded by a test to stand for what a previous process stored.
        self.restorable: list[Order] = []
        #: Seeded by a test to stand for the order table's whole history —
        #: rejections included, which is what `recent_orders` exists to surface
        #: and what `restorable` (non-terminal only) deliberately excludes.
        self.history: list[Order] = []

    async def save(self, order: Order, *, run_mode: object) -> None:
        self.save_calls.append(order.client_order_id)
        self.saved[order.client_order_id] = replace(order, fills=list(order.fills))

    async def open_orders(self, run_mode: object) -> list[Order]:
        return list(self.restorable)

    async def recent_orders(
        self,
        run_mode: object,
        *,
        status: OrderStatus | None = None,
        symbol: str | None = None,
        strategy_id: str | None = None,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[Order]:
        """The display read, over whatever `history` a test seeded.

        Filters and sorts here rather than returning the list untouched, so a
        test asserting on the *screen* is asserting against the same shape the
        real repository produces — newest first, bounded, filters composed.
        """
        matched = [
            order
            for order in self.history
            if (status is None or order.status is status)
            and (symbol is None or order.symbol == symbol.upper())
            and (strategy_id is None or order.strategy_id == strategy_id)
            and (since is None or (order.created_at is not None and order.created_at >= since))
        ]
        matched.sort(
            key=lambda o: (o.created_at or datetime.min.replace(tzinfo=UTC), o.id), reverse=True
        )
        return matched[:limit]


class FakePortfolioRepository:
    """In-memory `PortfolioRepository`.

    `latest` returns None until a test seeds `stored`, which is the first-boot
    case the worker adopts the broker's book for.
    """

    def __init__(self) -> None:
        self.snapshots: list[tuple[datetime, Portfolio]] = []
        self.stored: Portfolio | None = None
        #: When `stored` was written. Seeded by a test that cares how stale the
        #: book it is serving looks.
        self.stored_at: datetime = datetime(2026, 1, 1, tzinfo=UTC)
        #: Seeded by a test to stand for the `equity_snapshots` history the day
        #: anchor and the dashboard's chart are both read from.
        self.equity_points: list[EquityPoint] = []
        self.history_error: Exception | None = None

    async def snapshot(self, portfolio: Portfolio, *, at: datetime, run_mode: object) -> None:
        # Copied for the same reason as above: the runner keeps mutating this
        # portfolio, so a stored reference would appear to change after the
        # snapshot was taken.
        copy = Portfolio(cash=portfolio.cash, starting_equity=portfolio.starting_equity)
        for symbol, position in portfolio.positions.items():
            copy.positions[symbol] = replace(position)
        self.snapshots.append((at, copy))

    async def latest(self, run_mode: object) -> Portfolio | None:
        return self.stored

    async def latest_snapshot(self, run_mode: object) -> StoredBook | None:
        """`stored`, stamped with `stored_at`.

        The timestamp is seeded separately so a test can age the book
        deliberately — a stored book's age is the whole point of the read, and
        a fake that always returned "just now" could not exercise the case that
        matters, which is a worker that stopped hours ago.
        """
        if self.stored is None:
            return None
        return StoredBook(at=self.stored_at, portfolio=self.stored)

    async def equity_history(
        self, run_mode: object, *, start: datetime, end: datetime
    ) -> list[EquityPoint]:
        """Whatever a test seeded, filtered to the window.

        The filtering is real rather than a pass-through: `dashboard._day_pnl`
        takes the *first* point in the range as the day's anchor, so a fake that
        ignored `start` would hand back a point from before the session opened
        and make the assertion pass for the wrong reason.
        """
        if self.history_error is not None:
            raise self.history_error
        return [p for p in self.equity_points if start <= p.ts <= end]


class FakeStrategyRepository:
    """In-memory `StrategyRepository`.

    `ensure` is counted rather than merely recorded, because the property worth
    testing is that it is idempotent: the runner calls it at every session open,
    and a second call must not replace the row.
    """

    def __init__(self) -> None:
        self.stored: dict[str, StrategyRecord] = {}
        #: Seeded by a test to stand for the whole `strategies` table, which is
        #: what a reader sees. Distinct from `stored`, which holds the thinner
        #: record a worker writes.
        self.rows: list[StoredStrategy] = []
        self.ensure_calls: list[str] = []
        #: Set by a test to stand for a database that will not take the row. The
        #: runner must fail warmup rather than continue into an evaluation whose
        #: every write would be refused by a foreign key.
        self.ensure_error: Exception | None = None

    async def ensure(self, record: StrategyRecord) -> None:
        if self.ensure_error is not None:
            raise self.ensure_error
        self.ensure_calls.append(record.id)
        # Matches the real adapter: an existing row keeps the values it has.
        self.stored.setdefault(record.id, record)

    async def get(self, strategy_id: str) -> StrategyRecord | None:
        return self.stored.get(strategy_id)

    async def list_all(self, *, state: str | None = None) -> list[StoredStrategy]:
        """Whatever a test seeded in `rows`, filtered and ordered as the real
        adapter orders it — newest first by `created_at`.

        Seeded separately from `stored` because the two hold different types on
        purpose: `ensure` writes the thin `StrategyRecord` a worker knows, and
        this returns the whole row a reader needs.
        """
        matched = [row for row in self.rows if state is None or row.state == state]
        matched.sort(key=lambda r: (r.created_at, r.id), reverse=True)
        return matched


class FakeSignalRepository:
    """In-memory `SignalRepository`, upserting on the signal's id like the real one."""

    def __init__(self) -> None:
        self.stored: dict[str, tuple[Signal, SignalOutcome]] = {}
        self.save_calls: list[str] = []
        #: Set by a test to stand for a write that cannot land. Unlike the
        #: publisher's, this failure must reach the caller: the order saved a
        #: step later carries a foreign key to this row.
        self.save_error: Exception | None = None

    async def save(self, signal: Signal, outcome: SignalOutcome) -> None:
        if self.save_error is not None:
            raise self.save_error
        self.save_calls.append(signal.id)
        self.stored[signal.id] = (signal, outcome)

    async def recent(
        self, strategy_id: str | None = None, *, limit: int = 200
    ) -> list[tuple[Signal, SignalOutcome]]:
        rows = [
            pair
            for pair in self.stored.values()
            if strategy_id is None or pair[0].strategy_id == strategy_id
        ]
        rows.sort(key=lambda pair: pair[0].ts, reverse=True)
        return rows[:limit]

    async def between(
        self, start: datetime, end: datetime, *, strategy_id: str | None = None
    ) -> list[tuple[Signal, SignalOutcome]]:
        rows = [
            pair
            for pair in self.stored.values()
            if start <= pair[0].ts <= end
            and (strategy_id is None or pair[0].strategy_id == strategy_id)
        ]
        rows.sort(key=lambda pair: pair[0].ts)
        return rows


class FakeSnapshotStore:
    """In-memory `SnapshotStore`.

    `get` returns None until something is put, which is the state a dashboard
    meets when the worker is up but not trading — the case that must render
    banners and halts rather than an empty book.
    """

    def __init__(self) -> None:
        self.puts: list[LiveSnapshot] = []
        self.stored: dict[str, LiveSnapshot] = {}
        #: Set by a test to stand for an unreachable or unreadable Redis. The
        #: read path must fail the request rather than report an empty book.
        self.get_error: Exception | None = None
        self.put_error: Exception | None = None

    async def put(self, snapshot: LiveSnapshot) -> None:
        if self.put_error is not None:
            raise self.put_error
        self.puts.append(snapshot)
        self.stored[snapshot.run_mode.value] = snapshot

    async def get(self, run_mode: object) -> LiveSnapshot | None:
        if self.get_error is not None:
            raise self.get_error
        return self.stored.get(getattr(run_mode, "value", str(run_mode)))


class FakePublisher:
    """Records what would have gone out on Redis pub/sub."""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []
        self.error = error

    async def publish(self, channel: str, message: dict[str, Any]) -> None:
        if self.error is not None:
            raise self.error
        self.published.append((channel, message))

    def on(self, channel: str) -> list[dict[str, Any]]:
        return [m for c, m in self.published if c == channel]


class FakeBacktestRunRepository:
    """In-memory `BacktestRunRepository`.

    Mirrors the real adapter's one non-obvious behaviour: **every transition is
    conditional on the run still being in flight.** `mark_running`, `finish` and
    `fail` are no-ops against a run that is already `done` or `failed`, because
    arq can redeliver a job whose worker died and the second delivery must not
    overwrite a conclusion that has already been reached. A fake that let the
    last writer win would pass tests the Postgres adapter fails.
    """

    def __init__(self) -> None:
        self.runs: dict[str, StoredBacktestRun] = {}
        #: Set by a test to stand for a database that refuses the insert — a
        #: foreign key rejecting a strategy no worker has ever registered is the
        #: realistic case, and the endpoint has to answer something better than
        #: a constraint name.
        self.create_error: Exception | None = None
        #: Ids passed to `fail`, in order. The enqueue-failure path is the one
        #: worth asserting on: a run nothing will execute must not be left
        #: claiming to be queued.
        self.failed: list[str] = []

    async def create(self, run: StoredBacktestRun) -> None:
        if self.create_error is not None:
            raise self.create_error
        if run.id in self.runs:
            raise ValueError(f"duplicate backtest run id {run.id}")
        self.runs[run.id] = run

    def _in_flight(self, run_id: str) -> StoredBacktestRun | None:
        run = self.runs.get(run_id)
        return run if run is not None and run.is_in_flight else None

    async def mark_running(self, run_id: str, *, at: datetime) -> None:
        run = self._in_flight(run_id)
        if run is not None:
            self.runs[run_id] = replace(run, status="running", started_at=at, error=None)

    async def finish(
        self,
        run_id: str,
        *,
        at: datetime,
        metrics: dict[str, float],
        equity_curve: list[list[str]],
        trades: list[dict[str, object]],
    ) -> None:
        run = self._in_flight(run_id)
        if run is not None:
            self.runs[run_id] = replace(
                run,
                status="done",
                metrics=metrics,
                equity_curve=equity_curve,
                trades=trades,
                error=None,
                finished_at=at,
            )

    async def fail(self, run_id: str, *, at: datetime, error: str) -> None:
        run = self._in_flight(run_id)
        if run is None:
            return
        self.failed.append(run_id)
        # Results cleared, like the real adapter: a partial curve under a failed
        # status is a chart of part of what somebody asked about, which is worse
        # than no chart because it renders.
        self.runs[run_id] = replace(
            run,
            status="failed",
            error=error,
            metrics=None,
            equity_curve=None,
            trades=None,
            finished_at=at,
        )

    async def get(self, run_id: str) -> StoredBacktestRun | None:
        return self.runs.get(run_id)

    async def list_runs(
        self, *, strategy_id: str | None = None, limit: int = 50
    ) -> list[StoredBacktestRun]:
        matched = [
            run
            for run in self.runs.values()
            if strategy_id is None or run.spec.strategy_id == strategy_id
        ]
        matched.sort(key=lambda r: (r.queued_at, r.id), reverse=True)
        return matched[:limit]

    async def stale_running(self, *, older_than: datetime) -> list[str]:
        return [
            run.id
            for run in self.runs.values()
            if run.status == "running"
            and run.started_at is not None
            and run.started_at < older_than
        ]


class FakeBacktestQueue:
    """In-memory `BacktestQueue`.

    Two behaviours worth mirroring exactly, because tests turn on both:

    - **`enqueue` is idempotent on the run id.** The real one derives arq's job
      id from it, so a retried request cannot queue the same run twice. A fake
      that appended twice would let a duplicate-submission bug pass.
    - **`report` never raises.** Progress is a nicety and the real adapter
      swallows a store that is unreachable; `report_error` here is what a test
      sets to prove a run still completes when nothing can be published.
    """

    def __init__(self, *, enqueue_error: Exception | None = None) -> None:
        self.enqueued: list[str] = []
        self.enqueue_error = enqueue_error
        self.progress_by_run: dict[str, BacktestProgress] = {}
        self.reports: list[BacktestProgress] = []
        self.report_error: Exception | None = None

    async def enqueue(self, run_id: str) -> None:
        if self.enqueue_error is not None:
            raise self.enqueue_error
        if run_id not in self.enqueued:
            self.enqueued.append(run_id)

    async def report(self, progress: BacktestProgress) -> None:
        if self.report_error is not None:
            return  # swallowed, exactly as the real adapter swallows it
        self.reports.append(progress)
        self.progress_by_run[progress.run_id] = progress

    async def progress(self, run_id: str) -> BacktestProgress | None:
        return self.progress_by_run.get(run_id)
