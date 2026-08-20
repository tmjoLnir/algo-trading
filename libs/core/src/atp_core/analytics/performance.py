"""Analytics and reporting — requirement #6.

Runs over live/paper trading history using the same metric functions as the
backtest engine (`backtest/metrics.py`). Deliberately shared: the question worth
answering is "is live performing like the backtest said it would?", and two
separate implementations would make that comparison meaningless.

Pure, and it has to be (rule §1.3). Nothing here opens a connection: the caller
loads orders through `execution.ports.OrderRepository.filled_orders` and bars
through `data.ports.BarRepository`, and hands them in. That is also why MAE/MFE
is a second pass — `build_trades` cannot fetch the bars it would need, so it
returns trades without excursions and `with_excursions` fills them in from bars
the caller fetched for the windows the trades name.

**What a trade is here.** One *position episode*: flat, through however many
scale-ins and partial exits, back to flat. Not one order, and not one tax lot.
`docs/ANALYTICS.md` has the full argument and the worked examples; the short
version is that this is the unit a human reasons about, it is what makes
`exit_reason` a single answer, and it is what makes "the holding period" a
well-defined window to measure an excursion over.

That grouping narrows where the matching convention bites, and the stub this
replaced slightly overstated it. Within one episode, entries and exits are
aggregated, so FIFO and LIFO produce *identical* per-trade P&L — there is
nothing to choose between. The convention only binds on a fill that takes a
position **through** zero, where some of its quantity closes the old episode and
the rest opens the opposite one. That split is FIFO, and it is the case
CLAUDE.md §5 and docs/TESTING.md both single out.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import UTC
from decimal import Decimal
from itertools import pairwise
from statistics import median
from typing import TYPE_CHECKING

from atp_core.backtest.metrics import TRADING_DAYS_PER_YEAR, PerformanceMetrics, compute_all
from atp_core.execution.idempotency import (
    ENTRY,
    EXIT,
    FLATTEN,
    MANUAL,
    STOP_LOSS,
    TAKE_PROFIT,
    TIME_EXIT,
)
from atp_core.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from datetime import date, datetime

    from atp_core.domain import Bar, Order

log = get_logger(__name__)

#: What a trade's `strategy_id` says when the order that opened it named no
#: strategy — a manual order placed from the dashboard, or an order stored before
#: `orders.strategy_id` had anything to point at. A sentinel rather than an empty
#: string, so it renders as itself in an attribution table instead of as a blank
#: row nobody can interpret.
UNATTRIBUTED = "unattributed"

#: What `exit_reason` says when the closing order's purpose is not one this maps.
#: Reachable for real: orders stored before `orders.purpose` existed have none
#: (migration `c3f8b2d5e714`), and they are reported as unknown rather than
#: guessed into a bucket. A wrong exit reason is worse than a missing one — it is
#: the number that decides whether a strategy's stops are misplaced.
UNKNOWN_EXIT = "unknown"

#: `purpose` (`execution.idempotency`) → the exit vocabulary a human reads.
#:
#: Two entries need their reasoning stated. `flatten` and `manual` collapse:
#: both mean a person or an operator procedure closed this, which is one fact
#: about the trade however it was expressed. And `entry` maps to `signal`, which
#: looks like a category error and is not — an entry appears here only as the
#: *closing* leg of an episode, which happens when an order reverses a position
#: through zero. The strategy decided to go the other way, and that is a signal
#: exit as much as an explicit `EXIT` is. Leaving it unmapped would report the
#: long half of every reversal as `unknown`, which is a worse answer than the
#: one available.
_EXIT_REASONS = {
    ENTRY: "signal",
    EXIT: "signal",
    STOP_LOSS: "stop_loss",
    TAKE_PROFIT: "take_profit",
    TIME_EXIT: "time",
    FLATTEN: "manual",
    MANUAL: "manual",
}

#: Namespace for deriving a trade's id. Fixed, so reconstructing the same
#: history twice produces the same ids — a screen keyed on them must not have
#: every row change identity because somebody reloaded the page.
_TRADE_NAMESPACE = uuid.UUID("3f2504e0-4f89-11d3-9a0c-0305e82c3301")

#: Attribution dimensions, and the trade attribute each groups on.
ATTRIBUTION_DIMENSIONS = ("strategy", "symbol", "hour", "weekday", "exit_reason")

_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

#: A US equity session: 09:30–16:00, the same 6.5 hours `backtest.metrics`
#: assumes when it annualises minute bars at 252 × 390.
SECONDS_PER_SESSION = 6.5 * 3600
SECONDS_PER_DAY = 24 * 3600


@dataclass(frozen=True, slots=True)
class TradeRecord:
    """A completed round trip — entry through exit.

    Distinct from an `Order`: one trade may span several orders (scaling in,
    partial exits). Trade-level statistics are what a human reasons about;
    order-level ones are noise.
    """

    trade_id: str
    strategy_id: str
    symbol: str
    side: str
    entry_ts: datetime
    exit_ts: datetime | None
    entry_price: Decimal
    exit_price: Decimal | None
    qty: Decimal
    gross_pnl: Decimal
    fees: Decimal
    net_pnl: Decimal
    return_pct: Decimal
    holding_period_hours: float
    exit_reason: str  # "signal" | "stop_loss" | "take_profit" | "time" | "manual"
    max_favorable_excursion: Decimal | None = None
    max_adverse_excursion: Decimal | None = None

    @property
    def is_long(self) -> bool:
        return self.side == "long"

    @property
    def is_win(self) -> bool:
        """Net of fees, which is the only definition that pays anybody.

        A trade that made $3 gross and paid $4 in commission is a loss. Counting
        it as a win inflates the win rate on exactly the strategies whose edge
        is too thin to survive their own costs.
        """
        return self.net_pnl > 0


@dataclass(frozen=True, slots=True)
class AttributionRow:
    """P&L broken down by a dimension (strategy, symbol, hour, day of week)."""

    key: str
    net_pnl: Decimal
    num_trades: int
    win_rate: float
    avg_pnl: Decimal
    contribution_pct: float


@dataclass(slots=True)
class _Leg:
    """One side of an episode, accumulated across however many fills built it."""

    qty: Decimal = Decimal(0)
    notional: Decimal = Decimal(0)
    fees: Decimal = Decimal(0)
    first_ts: datetime | None = None
    last_ts: datetime | None = None

    def add(self, qty: Decimal, price: Decimal, fee: Decimal, ts: datetime) -> None:
        self.qty += qty
        self.notional += qty * price
        self.fees += fee
        if self.first_ts is None:
            self.first_ts = ts
        self.last_ts = ts

    @property
    def vwap(self) -> Decimal:
        """Volume-weighted average price, or zero for an empty leg.

        Zero is unreachable for a leg that exists — an episode is created by its
        first fill and a fill has positive quantity — so the guard is a
        div-by-zero belt rather than a meaningful value.
        """
        return self.notional / self.qty if self.qty else Decimal(0)


@dataclass(slots=True)
class _Episode:
    """A position from flat to flat, in the middle of being reconstructed."""

    symbol: str
    long: bool
    strategy_id: str
    entry: _Leg
    exit: _Leg
    exit_purpose: str | None = None

    @property
    def open_qty(self) -> Decimal:
        return self.entry.qty - self.exit.qty


class PerformanceAnalyzer:
    """Trade reconstruction, statistics and attribution over stored orders."""

    def build_trades(self, orders: list[Order]) -> list[TradeRecord]:
        """Fold fills into round trips.

        Reads the fills, not the orders' summary fields. `filled_qty` and
        `avg_fill_price` are running totals, so an order that filled in four
        prints across two sessions reports one average at one instant — and both
        the holding period and the excursion window are wrong if that is what
        you measure from.

        The subtle part is a fill that carries a position **through** zero. Some
        of its quantity closes the episode that existed and the rest opens the
        opposite one, and the split is FIFO: the closing part is whatever the old
        episode still had open, the remainder opens the new one. Both halves have
        to happen, and on the same fill — treating a flip as a plain reversal
        loses the closing trade entirely, and treating it as a close loses the
        new position, which is then invisible until it is exited and looks like
        an exit with no entry.

        Fees on such a fill are split pro rata by quantity. There is no better
        answer available: the venue charged one commission for one execution and
        nothing in the print says which part of it belonged to closing.

        Orders are expected oldest-first by decision instant, which is what
        `OrderRepository.filled_orders` returns. Within that, fills are ordered
        by their own timestamps, because that is the sequence the market
        actually printed.

        An episode still open when the history runs out is **not** returned. It
        is a position, not a round trip; reporting it as a trade closed at the
        last price would put an unrealised number in a table of realised ones.
        """
        trades: list[TradeRecord] = []
        for symbol, events in _fill_stream(orders).items():
            episode: _Episode | None = None
            for ts, signed_qty, price, fee, purpose, strategy_id in events:
                opening = signed_qty > 0
                remaining, remaining_fee = abs(signed_qty), fee

                if episode is not None and episode.long is not opening:
                    closing = min(remaining, episode.open_qty)
                    # Pro rata rather than all-on-the-close: see the docstring.
                    closing_fee = remaining_fee * closing / remaining if remaining else Decimal(0)
                    episode.exit.add(closing, price, closing_fee, ts)
                    episode.exit_purpose = purpose
                    remaining -= closing
                    remaining_fee -= closing_fee
                    if episode.open_qty == 0:
                        trades.append(_to_trade(episode))
                        episode = None

                if remaining == 0:
                    continue
                if episode is None:
                    episode = _Episode(
                        symbol=symbol,
                        long=opening,
                        strategy_id=strategy_id,
                        entry=_Leg(),
                        exit=_Leg(),
                    )
                episode.entry.add(remaining, price, remaining_fee, ts)

        # One list across every symbol, in the order the trades closed. A report
        # is read forwards in time, not grouped by ticker.
        trades.sort(key=lambda t: (t.exit_ts or t.entry_ts, t.symbol))
        return trades

    def with_excursions(
        self, trades: list[TradeRecord], bars_by_symbol: Mapping[str, Sequence[Bar]]
    ) -> list[TradeRecord]:
        """Fill in MAE/MFE from bars covering each trade's holding period.

        Separate from `build_trades` because it needs data that module cannot
        fetch (rule §1.3). The caller reads bars for each trade's symbol over
        `[entry_ts, exit_ts]` and passes them here.

        MAE and MFE are P&L, not distances: the glossary defines them as the
        worst and best *unrealised P&L* during the trade, so both are scaled by
        the position's quantity. MFE is ≥ 0 and MAE is ≤ 0 by construction — a
        trade that only ever went one way has zero on the other side, which is a
        measurement rather than a missing value.

        A trade with no bars in its window gets `None` on both, never zero. Zero
        says "this trade never moved against us", which is the most flattering
        possible reading of "we have no idea". `atp_api.routers.dashboard` makes
        the same distinction for the same reason.

        **At bar resolution these are bounds, not measurements.** The bar
        covering the entry has a low and a high that may have printed before we
        were filled, so its extremes are attributed to a position that did not
        yet exist. The error is one bar wide at each end and always in the
        direction of a *larger* excursion, so MAE read here is the pessimistic
        end of the range — which is the right direction for a number used to
        decide whether a stop sits too close. Minute bars narrow it; intrabar
        data removes it.
        """
        out: list[TradeRecord] = []
        for trade in trades:
            window = _bars_in(bars_by_symbol.get(trade.symbol, ()), trade)
            if not window:
                out.append(trade)
                continue

            high = max(bar.high for bar in window)
            low = min(bar.low for bar in window)
            if trade.is_long:
                best, worst = high - trade.entry_price, low - trade.entry_price
            else:
                best, worst = trade.entry_price - low, trade.entry_price - high

            out.append(
                replace(
                    trade,
                    # Clamped at zero on each side rather than allowed to cross.
                    # A gap that opened past the entry and never came back makes
                    # the raw "best" negative, and a *negative* maximum
                    # favourable excursion is not a quantity anyone can read.
                    max_favorable_excursion=max(best, Decimal(0)) * trade.qty,
                    max_adverse_excursion=min(worst, Decimal(0)) * trade.qty,
                )
            )
        return out

    def metrics(
        self,
        trades: list[TradeRecord],
        equity_curve: list[tuple[datetime, Decimal]],
        *,
        periods_per_year: int | None = None,
    ) -> PerformanceMetrics:
        """The full metric set, through the backtest's own functions.

        `compute_all` is shared with the backtest deliberately (ADR 0006's
        reasoning, a fourth time): a live Sharpe computed by different code from
        the backtested one cannot be compared to it, and comparing them is the
        whole point of running paper first.

        `periods_per_year` annualises, and getting it wrong is the one way to
        make every ratio here meaningless while all of them still look
        plausible. It is **inferred from the curve's own spacing** when not
        given, because the caller most likely to be wrong is the one that does
        not think about it: the runner writes an equity point per evaluation —
        once a minute — and annualising a minute-sampled series as though it were
        daily understates volatility by about twenty times, which turns a
        mediocre Sharpe into a spectacular one. An explicit value always wins;
        see `infer_periods_per_year` for what inference refuses to do.

        The three trade-shaped metrics `compute_all` cannot derive from an equity
        curve are supplied from the trades: how long positions were held, how
        much of the period the book was exposed, and how much notional turned
        over.
        """
        periods = (
            periods_per_year
            if periods_per_year is not None
            else infer_periods_per_year(equity_curve)
        )
        holding = [t.holding_period_hours for t in trades]
        return compute_all(
            [(ts, equity) for ts, equity in equity_curve],
            [t.net_pnl for t in trades],
            periods_per_year=periods,
            avg_holding_period_hours=sum(holding) / len(holding) if holding else 0.0,
            exposure_pct=_exposure_pct(trades, equity_curve),
            turnover=_turnover(trades, equity_curve),
        )

    def attribution(self, trades: list[TradeRecord], by: str) -> list[AttributionRow]:
        """Group P&L by `by` ∈ {strategy, symbol, hour, weekday, exit_reason}.

        `exit_reason` is the most actionable: a strategy whose profit comes
        entirely from take-profits while stops bleed it has a stop-placement
        problem, not a signal problem.

        `hour` and `weekday` group on the **entry**, because the question they
        answer is when this strategy finds trades worth taking. Grouping on the
        exit would answer when its stops happen to fire, which is a fact about
        the market's schedule rather than about the strategy.

        Ordered by net P&L, best first — a report is read from the top, and the
        top is where the money is.

        An unrecognised dimension raises rather than returning an empty list. A
        report silently grouped by nothing looks like a period with no trades,
        and "we made nothing" and "you asked for something that does not exist"
        are not the same answer.
        """
        if by not in ATTRIBUTION_DIMENSIONS:
            raise ValueError(
                f"cannot attribute by {by!r}; known dimensions are "
                f"{', '.join(ATTRIBUTION_DIMENSIONS)}"
            )

        grouped: dict[str, list[TradeRecord]] = {}
        for trade in trades:
            grouped.setdefault(_attribution_key(trade, by), []).append(trade)

        # Denominated in the total *absolute* P&L rather than the net. Net is the
        # intuitive denominator and it misbehaves exactly when a reader most
        # needs the number: a period whose winners and losers nearly cancel has a
        # near-zero net, and a share of it reads as +900% for one strategy and
        # −800% for another. Absolute P&L is always at least as large as the net,
        # so every contribution lands inside ±100% and the signs still say who
        # helped and who hurt.
        gross_magnitude = sum((abs(t.net_pnl) for t in trades), Decimal(0))

        rows = [
            AttributionRow(
                key=key,
                net_pnl=sum((t.net_pnl for t in group), Decimal(0)),
                num_trades=len(group),
                win_rate=sum(1 for t in group if t.is_win) / len(group),
                avg_pnl=sum((t.net_pnl for t in group), Decimal(0)) / len(group),
                contribution_pct=(
                    float(sum((t.net_pnl for t in group), Decimal(0)) / gross_magnitude * 100)
                    if gross_magnitude
                    else 0.0
                ),
            )
            for key, group in grouped.items()
        ]
        rows.sort(key=lambda r: r.net_pnl, reverse=True)
        return rows

    def daily_returns(self, equity_curve: list[tuple[datetime, Decimal]]) -> dict[date, Decimal]:
        """Close-over-close return for each day the curve covers.

        `Decimal` rather than float because this divides two account balances,
        and the result is reported as a percentage a human reconciles against a
        broker statement (rule §1.1).

        The day is the **UTC calendar day**, and for US equities that is the
        session: the cash market runs 13:30–21:00 UTC at the widest, so a
        session never straddles UTC midnight and no calendar is needed to group
        by it. A market that does straddle it — an overnight future, an Asian
        session read from a US-hosted worker — would need the exchange's own
        trading day, and this would silently split one session in two. Named
        rather than handled, because nothing in this platform trades one yet.

        The first day has no prior close, so it has **no return** and is absent
        from the result rather than present as zero. A zero says the account was
        flat that day, which is a claim about the strategy; absence says the
        series started there.

        A day whose prior close was zero or negative is also absent: the account
        was already gone, and there is no return to compute from it. Same
        reasoning as `metrics.returns_from_equity`, which yields 0 there because
        it returns a dense array; a dict can say "not defined" properly.
        """
        closes: dict[date, Decimal] = {}
        for ts, equity in equity_curve:
            # Last point of each day wins — the curve is chronological, so this
            # leaves the day's close in the map.
            closes[ts.astimezone(UTC).date()] = equity

        days = sorted(closes)
        return {
            day: (closes[day] - closes[previous]) / closes[previous]
            for previous, day in pairwise(days)
            if closes[previous] > 0
        }

    def compare_to_backtest(
        self,
        live: PerformanceMetrics | Mapping[str, float | int | None],
        backtest: PerformanceMetrics | Mapping[str, float | int | None],
    ) -> dict[str, float | None]:
        """Live vs backtest, metric by metric.

        Large negative divergence usually means one of: overfitting, unmodelled
        costs, or a strategy whose backtested fills were unachievable. Surfacing
        it is the single most valuable report this platform produces.

        Each value is `live - backtest`: a difference, not a ratio. A ratio is
        undefined against a backtested Sharpe of zero and inverts its meaning
        against a negative one, and neither failure would be obvious in a table.
        A difference is defined everywhere and reads the way the question is
        asked — how much worse is this than it promised?

        Every field of `PerformanceMetrics` is compared, including
        `num_trades`. A live run that took a third as many trades as its
        backtest has not underperformed; it has been refused, and that shows up
        here before anyone starts blaming the signal.

        **Either side may be a plain mapping, and a value in one may be None.**
        That is not defensive typing; it is the shape a *stored* backtest
        actually has. `backtest.runner.jsonable` nulls every non-finite metric on
        the way into the `backtest_runs` row, because `Infinity` is not legal
        JSON — and an infinite `profit_factor` means no losing trade, which is
        precisely the run somebody wants to hold a live record up against. Read
        back as a `PerformanceMetrics` those nulls would have to be guessed into
        floats; subtracted raw they would raise. Both are worse than reporting
        the one honest answer, which is that this metric has no comparison.

        A None divergence therefore means **not available**, never zero — the
        same convention the metric column itself carries. A metric absent from
        one side entirely gets the same treatment, so a metric set that grows a
        field does not make every older stored run uncomparable.
        """
        mine = live.to_dict() if isinstance(live, PerformanceMetrics) else dict(live)
        theirs = backtest.to_dict() if isinstance(backtest, PerformanceMetrics) else dict(backtest)
        return {
            name: _difference(mine.get(name), theirs.get(name)) for name in mine.keys() | theirs
        }


#: Live trades below which the live half of a comparison is too small a sample
#: to conclude anything from. docs/BACKTESTING.md's own threshold, and the same
#: one `backtest.runner.suspicious` applies to a backtest — deliberately, because
#: "thirty trades" is a fact about statistics rather than about which side of the
#: comparison you are on.
MIN_COMPARABLE_TRADES = 30

#: How far two windows may differ in length before their window-basis metrics
#: stop being worth subtracting. A live month against a backtested five years
#: produces a `total_return` divergence of almost exactly minus the backtest's
#: return, whatever the strategy did.
WINDOW_LENGTH_TOLERANCE = 0.5


def comparability_warnings(
    *,
    live_periods_per_year: int,
    backtest_periods_per_year: int,
    live_days: float | None,
    backtest_days: float,
    live_trades: int,
    live_symbols: Sequence[str],
    backtest_symbols: Sequence[str],
) -> list[str]:
    """Reasons a divergence in this comparison is not what it looks like.

    Computed here and returned with the numbers, for the reason
    `backtest.runner.suspicious` gives for doing the same to a backtest: a number
    a human has already read is a number they have already believed. A
    divergence table is read by somebody deciding whether to keep a strategy
    running with real money, and the failure mode is not that they distrust a
    real divergence — it is that they act on an artefact.

    Not a refusal, and deliberately not a filter. Every metric is still
    subtracted and returned; these sentences say which of the answers to weigh.
    `METRIC_BASIS` is the other half — it says which metrics each sentence
    reaches.
    """
    notes: list[str] = []

    if live_trades == 0:
        notes.append(
            "no live round trips closed in this window, so every live metric is "
            "the value of an empty series rather than a measurement. The "
            "divergence below is the backtest's own numbers, negated"
        )
    elif live_trades < MIN_COMPARABLE_TRADES:
        notes.append(
            f"only {live_trades} live round trips — under about "
            f"{MIN_COMPARABLE_TRADES} the statistics mean very little "
            "(docs/BACKTESTING.md 'Reading the result')"
        )

    if live_periods_per_year != backtest_periods_per_year:
        notes.append(
            f"the two sides are annualised on different bases — live at "
            f"{live_periods_per_year} periods a year, the backtest at "
            f"{backtest_periods_per_year}. The live curve steps once per closed "
            f"trade and the backtest's once per bar, so every annualised metric "
            f"(see the comparability of each) differs partly for that reason "
            f"alone. Pin `periods_per_year` to compare them on one basis"
        )

    if live_days is not None and live_days > 0 and backtest_days > 0:
        shorter, longer = sorted((live_days, backtest_days))
        if shorter / longer < WINDOW_LENGTH_TOLERANCE:
            notes.append(
                f"the windows are different lengths — {live_days:.1f} days live "
                f"against {backtest_days:.1f} backtested. Every window-basis "
                f"metric scales with that and is not a like-for-like difference"
            )

    live_only = sorted(set(live_symbols) - set(backtest_symbols))
    if live_only:
        notes.append(
            f"live traded {', '.join(live_only)}, which this backtest never "
            f"covered. Their P&L is in the live metrics and has no counterpart "
            f"in the backtest's"
        )

    backtest_only = sorted(set(backtest_symbols) - set(live_symbols))
    if backtest_only and live_trades:
        notes.append(
            f"the backtest covered {', '.join(backtest_only)} and live has "
            f"closed no round trip in them. A strategy that is not trading a "
            f"symbol it was approved on is a refusal or a data gap, not "
            f"underperformance — check `/analytics/attribution` and the signals "
            f"table before reading the trade count below"
        )

    notes.append(
        "the backtest sized every entry at a flat share count and live sizing is "
        "risk-based (docs/RISK.md 'Position sizing'), so the money-denominated "
        "metrics — expectancy, the win and loss sizes, turnover, total return — "
        "are partly a difference between two sizing rules"
    )
    return notes


def _difference(live: float | int | None, backtest: float | int | None) -> float | None:
    """`live - backtest`, or None when either side does not have the number.

    Not zero. A zero divergence is the strongest possible claim this report can
    make — the strategy performed exactly as promised on this metric — and it is
    the last thing a missing value should be rendered as.
    """
    if live is None or backtest is None:
        return None
    return float(live) - float(backtest)


def infer_periods_per_year(equity_curve: Sequence[tuple[datetime, Decimal]]) -> int:
    """How many samples a year this curve is spaced at, from the curve itself.

    The **median** gap, not the mean: an equity curve has gaps at every weekend
    and every overnight, and a mean over a minute-sampled series with 16-hour
    holes in it lands nowhere near either. The median gap of a series sampled
    each minute during sessions is one minute.

    **Two regimes, because a trading year is not a calendar year.** A gap
    shorter than a day is a within-session sampling rate, so it divides into the
    6.5-hour session and multiplies by 252 — a minute-sampled curve is 252 × 390
    a year. A gap of a day or more is one sample per N days, so it divides into
    252 directly. One expression cannot serve both: dividing a trading year's
    *seconds* by a daily gap counts the 17.5 hours a day when nothing is sampled
    and reports a daily curve as 68 periods a year.

    The day-or-more branch measures in calendar days, which is exact at one
    sample a day and approximate beyond it — a weekly curve reads 36 rather than
    the 50 its five-trading-day week deserves. Naming it rather than fixing it:
    the fix needs a calendar, this module is pure, and nothing in this platform
    samples equity weekly. Pass `periods_per_year` explicitly for such a series.

    Falls back to daily (252) for a curve with fewer than three points or with a
    non-positive median gap. Two points have one gap and no way to tell a
    sampling interval from a coincidence, and this is the one place a fallback is
    safe: annualising a two-point curve produces a meaningless ratio whichever
    number goes in, and `compute_all` already returns 0.0 for most statistics
    over a series that short.
    """
    if len(equity_curve) < 3:
        return TRADING_DAYS_PER_YEAR
    gaps = [
        (later - earlier).total_seconds() for (earlier, _), (later, _) in pairwise(equity_curve)
    ]
    typical = median(gaps)
    if typical <= 0:
        return TRADING_DAYS_PER_YEAR

    if typical >= SECONDS_PER_DAY:
        return max(1, round(TRADING_DAYS_PER_YEAR / (typical / SECONDS_PER_DAY)))
    return max(1, round(TRADING_DAYS_PER_YEAR * SECONDS_PER_SESSION / typical))


def _fill_stream(
    orders: Iterable[Order],
) -> dict[str, list[tuple[datetime, Decimal, Decimal, Decimal, str, str]]]:
    """Every fill, grouped by symbol and ordered as the market printed them.

    The tuple carries the fill plus the two things about its *order* a trade
    needs and a fill does not know: what the order was for, and which strategy
    asked for it.

    Sorted by fill timestamp, with the order's position in the input as the
    tie-break. Two prints can share a timestamp — a market order sweeping two
    price levels within the same microsecond — and the input order is by
    decision instant, so it is the better guess than an arbitrary one.
    """
    streams: dict[str, list[tuple[datetime, Decimal, Decimal, Decimal, str, str]]] = {}
    keyed: dict[str, list[tuple[datetime, int, int]]] = {}
    for index, order in enumerate(orders):
        sign = order.side.sign
        strategy = order.strategy_id or UNATTRIBUTED
        for position, fill in enumerate(order.fills):
            streams.setdefault(order.symbol, []).append(
                (fill.ts, fill.qty * sign, fill.price, fill.fee, order.purpose, strategy)
            )
            keyed.setdefault(order.symbol, []).append((fill.ts, index, position))

    for symbol, stream in streams.items():
        order_keys = keyed[symbol]
        combined = sorted(zip(order_keys, stream, strict=True), key=lambda pair: pair[0])
        streams[symbol] = [event for _, event in combined]
    return streams


def _to_trade(episode: _Episode) -> TradeRecord:
    """Close out an episode into a `TradeRecord`.

    Called only when `open_qty` has reached zero, so entry and exit quantities
    agree and the P&L is fully realised.
    """
    entry_price, exit_price = episode.entry.vwap, episode.exit.vwap
    qty = episode.entry.qty
    direction = Decimal(1) if episode.long else Decimal(-1)

    # Signed by direction so one expression covers both sides. A short that sold
    # for 1,000 and bought back for 900 made 100: (900 − 1,000) × −1.
    gross = (episode.exit.notional - episode.entry.notional) * direction
    fees = episode.entry.fees + episode.exit.fees
    net = gross - fees

    entry_ts = episode.entry.first_ts
    exit_ts = episode.exit.last_ts
    assert entry_ts is not None and exit_ts is not None  # an episode has both legs

    return TradeRecord(
        trade_id=_trade_id(episode.symbol, entry_ts, exit_ts, episode.long),
        strategy_id=episode.strategy_id,
        symbol=episode.symbol,
        side="long" if episode.long else "short",
        entry_ts=entry_ts,
        exit_ts=exit_ts,
        entry_price=entry_price,
        exit_price=exit_price,
        qty=qty,
        gross_pnl=gross,
        fees=fees,
        net_pnl=net,
        # Against the capital the position committed — the notional put up for a
        # long, the proceeds taken in for a short. Net of fees, because the
        # return anybody experienced is the one after costs.
        return_pct=(net / episode.entry.notional) if episode.entry.notional else Decimal(0),
        holding_period_hours=(exit_ts - entry_ts).total_seconds() / 3600,
        exit_reason=_EXIT_REASONS.get(episode.exit_purpose or "", UNKNOWN_EXIT),
    )


def _trade_id(symbol: str, entry_ts: datetime, exit_ts: datetime, long: bool) -> str:
    """A stable id for one round trip.

    Derived rather than random, for the same reason `client_order_id` is: the
    same history reconstructed twice must produce the same ids, or every row on
    a screen changes identity when somebody reloads it. Symbol, both instants and
    the side are enough — one symbol cannot have two episodes of the same side
    that opened and closed at the same two instants.
    """
    side = "long" if long else "short"
    return str(
        uuid.uuid5(
            _TRADE_NAMESPACE,
            f"{symbol}:{side}:{entry_ts.astimezone(UTC).isoformat()}:"
            f"{exit_ts.astimezone(UTC).isoformat()}",
        )
    )


def _bars_in(bars: Sequence[Bar], trade: TradeRecord) -> list[Bar]:
    """The bars covering a trade's holding period, inclusive of both ends."""
    end = trade.exit_ts
    if end is None:
        return []
    return [bar for bar in bars if trade.entry_ts <= bar.ts <= end]


def _attribution_key(trade: TradeRecord, by: str) -> str:
    if by == "strategy":
        return trade.strategy_id
    if by == "symbol":
        return trade.symbol
    if by == "exit_reason":
        return trade.exit_reason
    if by == "hour":
        # Zero-padded so a string sort over the keys is a chronological one.
        return f"{trade.entry_ts.astimezone(UTC).hour:02d}"
    return _WEEKDAYS[trade.entry_ts.astimezone(UTC).weekday()]


def _exposure_pct(
    trades: Sequence[TradeRecord], equity_curve: Sequence[tuple[datetime, Decimal]]
) -> float:
    """Fraction of the reported period during which the book held something.

    Overlapping trades are merged rather than summed. Two positions held over
    the same hour is one hour of exposure — summing them gives 200% of a period,
    which is a leverage statement dressed up as a time one.

    Zero for a period with no trades or no span, which is a measurement here
    rather than a missing value: a book that traded nothing was exposed for none
    of it.
    """
    if len(equity_curve) < 2 or not trades:
        return 0.0
    span = (equity_curve[-1][0] - equity_curve[0][0]).total_seconds()
    if span <= 0:
        return 0.0

    intervals = sorted(
        (t.entry_ts, t.exit_ts) for t in trades if t.exit_ts is not None and t.exit_ts > t.entry_ts
    )
    covered, current = 0.0, None
    for start, end in intervals:
        if current is None:
            current = (start, end)
            continue
        if start <= current[1]:
            current = (current[0], max(current[1], end))
        else:
            covered += (current[1] - current[0]).total_seconds()
            current = (start, end)
    if current is not None:
        covered += (current[1] - current[0]).total_seconds()

    # Capped at 1.0: trades can start before the equity curve's first point when
    # the caller windowed one and not the other, and an exposure above 100% of
    # the period would be read as leverage.
    return min(covered / span, 1.0)


def _turnover(
    trades: Sequence[TradeRecord], equity_curve: Sequence[tuple[datetime, Decimal]]
) -> float:
    """Traded notional over average equity.

    Both legs of every round trip count: a position bought and sold has turned
    over twice its notional, which is what the two commissions were charged on.

    Zero when there is no equity to divide by, rather than infinity. An account
    with no equity did not trade at any rate; `compute_all` treats a missing
    value here as a gap in the report and so does this.
    """
    if not trades or not equity_curve:
        return 0.0
    equities = [float(equity) for _, equity in equity_curve if equity > 0]
    if not equities:
        return 0.0
    traded = sum(
        float(t.entry_price * t.qty) + float((t.exit_price or Decimal(0)) * t.qty) for t in trades
    )
    return traded / (sum(equities) / len(equities))
