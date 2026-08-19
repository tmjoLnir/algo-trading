"""Analytics and reporting endpoints — requirement #6.

These read history rather than serving the live book, which is what makes them a
different shape from `dashboard.py` and worth stating.

The dashboard serves a snapshot the *worker* computed, because two processes
computing "what do we hold" at two instants disagree (ADR 0007). Nothing like
that applies here: a closed round trip is finished, its numbers do not move, and
reconstructing them in this process cannot disagree with anything the runner is
doing. So these endpoints do the work themselves — load the orders, fold them
into trades, compute over them — and the runner stays out of it. It has enough
to do inside a one-minute tick without also producing reports nobody is reading
between polls.

**Reconstruction reads from the beginning, then filters.** Round trips are
matched from flat, so `OrderRepository.filled_orders` is bounded at the end only
and the window is applied to the *trades* that come out. A window applied to the
orders going in would present every position opened before it as an exit with no
entry. `docs/ANALYTICS.md` records the cost and what to do about it when it stops
being affordable.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from atp_api.deps import get_bar_repository, get_clock, get_order_repository
from atp_core.analytics.performance import (
    ATTRIBUTION_DIMENSIONS,
    PerformanceAnalyzer,
    TradeRecord,
    infer_periods_per_year,
)
from atp_core.clock import Clock
from atp_core.config import Settings, get_settings
from atp_core.data.ports import BarRepository
from atp_core.domain import Bar, Timeframe
from atp_core.execution.ports import OrderRepository
from atp_core.logging import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/analytics", tags=["analytics"])

#: How far back a request reaches when it names no start. A month is the period
#: an operator asks about without thinking, and long enough that a paper week
#: fits inside it whole.
DEFAULT_LOOKBACK_DAYS = 30

#: Bars are fetched per symbol to measure excursions, so a request covering many
#: symbols is many queries. Capped, and the response says when the cap bound —
#: silently returning trades with null excursions would read as "no bars stored"
#: rather than "we did not look".
MAX_EXCURSION_SYMBOLS = 50


class TradeView(BaseModel):
    """One completed round trip.

    Every monetary field is a `Decimal` and reaches the browser as a string —
    the dashboard performs no arithmetic on money, so nothing downstream ever
    parses one back (docs/DASHBOARD.md).
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
    exit_reason: str
    #: Null rather than zero when no bars covered the holding period. Zero would
    #: say the trade never moved against us, which is the most flattering
    #: possible reading of "we have no idea".
    max_favorable_excursion: Decimal | None
    max_adverse_excursion: Decimal | None


class TradesResponse(BaseModel):
    trades: list[TradeView]
    #: True when excursions were not measured because the request spanned more
    #: symbols than `MAX_EXCURSION_SYMBOLS`. Stated rather than left to be
    #: inferred from a column of nulls.
    excursions_omitted: bool
    start: datetime
    end: datetime


class PerformanceResponse(BaseModel):
    """The metric set, plus what it was computed over.

    `num_trades` is inside `metrics` already; `start`, `end` and `equity_points`
    are here because a Sharpe over four days and a Sharpe over four months are
    different claims and the response should not make a reader guess which one
    they have.
    """

    metrics: dict[str, float | int]
    start: datetime
    end: datetime
    equity_points: int
    #: What the annualisation used. Inferred from the curve's own spacing unless
    #: the caller pinned it, and worth returning: every ratio below scales with
    #: it, so a reader who disagrees with a Sharpe should be able to see this
    #: number before doubting the arithmetic.
    periods_per_year: int


class AttributionRowView(BaseModel):
    key: str
    net_pnl: Decimal
    num_trades: int
    win_rate: float
    avg_pnl: Decimal
    contribution_pct: float


class AttributionResponse(BaseModel):
    by: str
    rows: list[AttributionRowView]
    start: datetime
    end: datetime


def _window(start: date | None, end: date | None, now: datetime) -> tuple[datetime, datetime]:
    """Turn two optional dates into a closed instant range.

    The end date is inclusive — a request for `end=2026-08-19` means "through
    the nineteenth", not "up to midnight on the nineteenth". A range that
    silently dropped the last day would leave today's trades out of every report
    asked for today, which is when most of them are asked for.

    UTC, matching everything else stored. `PerformanceAnalyzer.daily_returns`
    documents where a UTC day and a trading session part company; for US
    equities they do not.

    `now` is passed in rather than read here, because rule §1.2 has no
    exemption for a default: a handler reading the wall clock directly is one
    a `SimulatedClock` cannot pin, and "which month did this report cover?"
    would then be unanswerable from a test.
    """
    resolved_end = end or now.astimezone(UTC).date()
    resolved_start = start or (resolved_end - timedelta(days=DEFAULT_LOOKBACK_DAYS))
    if resolved_start > resolved_end:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"start {resolved_start} is after end {resolved_end}",
        )
    return (
        datetime.combine(resolved_start, time.min, tzinfo=UTC),
        datetime.combine(resolved_end, time.max, tzinfo=UTC),
    )


def _in_window(trades: list[TradeRecord], start: datetime, end: datetime) -> list[TradeRecord]:
    """Trades that *closed* inside the window.

    On the exit rather than the entry, because a round trip belongs to the
    period whose P&L it landed in. A position opened in March and closed in
    August made its money in August, and attributing it to March would put a
    realised gain in a month whose reported total does not contain it.
    """
    return [t for t in trades if t.exit_ts is not None and start <= t.exit_ts <= end]


async def _reconstruct(
    order_repo: OrderRepository,
    settings: Settings,
    *,
    end: datetime,
    strategy_id: str | None,
) -> list[TradeRecord]:
    """Every completed round trip up to `end`, oldest first."""
    orders = await order_repo.filled_orders(settings.run_mode, until=end, strategy_id=strategy_id)
    return PerformanceAnalyzer().build_trades(orders)


@router.get("/performance")
async def get_performance(
    order_repo: Annotated[OrderRepository, Depends(get_order_repository)],
    settings: Annotated[Settings, Depends(get_settings)],
    clock: Annotated[Clock, Depends(get_clock)],
    start: date | None = None,
    end: date | None = None,
    strategy_id: str | None = None,
    periods_per_year: Annotated[int | None, Query(ge=1)] = None,
) -> PerformanceResponse:
    """Full metric set over a period.

    The equity curve comes from the trades' own P&L rather than from
    `equity_snapshots`, and that is a deliberate narrowing worth reading. The
    snapshot table is sampled per evaluation and includes marks on *open*
    positions, so a curve drawn from it moves on unrealised P&L — which is the
    right curve for the dashboard's chart and the wrong one for a statistic
    about closed trades, because it would attribute a metric to a period the
    trade had not yet resolved in. Here the curve steps only when a round trip
    closes.

    The consequence is stated rather than hidden: `max_drawdown` measured this
    way is the drawdown of *realised* P&L, which is shallower than the drawdown
    an account actually experienced. `/dashboard/equity-curve` is the series
    that answers the other question.
    """
    window_start, window_end = _window(start, end, clock.now())
    trades = _in_window(
        await _reconstruct(order_repo, settings, end=window_end, strategy_id=strategy_id),
        window_start,
        window_end,
    )

    curve = _realised_curve(trades, window_start)
    analyzer = PerformanceAnalyzer()
    metrics = analyzer.metrics(trades, curve, periods_per_year=periods_per_year)
    return PerformanceResponse(
        metrics=metrics.to_dict(),
        start=window_start,
        end=window_end,
        equity_points=len(curve),
        periods_per_year=(
            periods_per_year if periods_per_year is not None else infer_periods_per_year(curve)
        ),
    )


def _realised_curve(trades: list[TradeRecord], start: datetime) -> list[tuple[datetime, Decimal]]:
    """A cumulative P&L curve that steps once per closed trade.

    Anchored at zero at the window's start rather than at an account balance,
    because this is a P&L series and not a balance series. Every ratio
    `compute_all` derives from it — total return, CAGR, drawdown — is therefore
    relative to the period's own starting point.

    Starting at exactly zero would make `total_return` a division by zero, which
    `compute_all` guards but reports as 0.0. Anchored at the summed magnitude of
    the period's own trades instead: a notional stake large enough that the
    ratios are proportions of what was risked rather than of an arbitrary
    constant. With no trades there is nothing to anchor and the curve is empty,
    which every statistic already handles.
    """
    if not trades:
        return []
    stake = sum((abs(t.entry_price * t.qty) for t in trades), Decimal(0)) / len(trades)
    if stake <= 0:
        return []
    running = stake
    curve = [(start, running)]
    for trade in trades:
        running += trade.net_pnl
        # `exit_ts` is not None: `_in_window` selected on it.
        assert trade.exit_ts is not None
        curve.append((trade.exit_ts, running))
    return curve


@router.get("/trades")
async def list_trades(
    order_repo: Annotated[OrderRepository, Depends(get_order_repository)],
    bar_repo: Annotated[BarRepository, Depends(get_bar_repository)],
    settings: Annotated[Settings, Depends(get_settings)],
    clock: Annotated[Clock, Depends(get_clock)],
    start: date | None = None,
    end: date | None = None,
    strategy_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    timeframe: Timeframe = Timeframe.D1,
) -> TradesResponse:
    """Completed round trips with MAE/MFE.

    Newest first — a trade list is read to see what just happened — and capped,
    because this is a screen rather than an export.

    Excursions are measured after the cap is applied, so a request for 50 trades
    fetches bars for the symbols in those 50 rather than for every symbol in the
    period.

    `timeframe` says which stored series to measure the excursions against, and
    it is a parameter rather than a constant because the answer changes with it:
    a daily bar's high is the day's high, so MAE read from dailies is coarser
    than the same trade measured on minutes. It defaults to `1d` to match what
    `atp_worker.trading.build_runner` ingests; a deployment storing minute bars
    should ask for them and will get a tighter number.
    """
    window_start, window_end = _window(start, end, clock.now())
    trades = _in_window(
        await _reconstruct(order_repo, settings, end=window_end, strategy_id=strategy_id),
        window_start,
        window_end,
    )
    # Newest first, then capped — so the cap keeps the most recent trades rather
    # than the first ones the reconstruction happened to close.
    trades.sort(key=lambda t: t.exit_ts or t.entry_ts, reverse=True)
    trades = trades[:limit]

    analyzer = PerformanceAnalyzer()
    symbols = {t.symbol for t in trades}
    omitted = len(symbols) > MAX_EXCURSION_SYMBOLS
    if not omitted and trades:
        trades = analyzer.with_excursions(trades, await _bars_for(bar_repo, trades, timeframe))
    elif omitted:
        log.info(
            "analytics.excursions_omitted",
            symbols=len(symbols),
            cap=MAX_EXCURSION_SYMBOLS,
            msg="MAE/MFE not measured for this request",
        )

    return TradesResponse(
        # `asdict`, not `vars`: these are slotted dataclasses and have no
        # `__dict__` to read.
        trades=[TradeView(**asdict(t)) for t in trades],
        excursions_omitted=omitted,
        start=window_start,
        end=window_end,
    )


async def _bars_for(
    bar_repo: BarRepository, trades: list[TradeRecord], timeframe: Timeframe
) -> dict[str, list[Bar]]:
    """Bars covering every trade's holding period, one query per symbol.

    The window per symbol is the union of that symbol's trades — first entry to
    last exit — rather than one query per trade. A symbol traded forty times in
    a month is one range read instead of forty, and the analyzer slices each
    trade's own window out of it.

    A symbol whose read fails is skipped rather than failing the request: an
    excursion is a column on a table, and losing it must not lose the P&L
    figures beside it. The trade comes back with nulls, which the response
    already distinguishes from zero.
    """
    windows: dict[str, tuple[datetime, datetime]] = {}
    for trade in trades:
        if trade.exit_ts is None:
            continue
        low, high = windows.get(trade.symbol, (trade.entry_ts, trade.exit_ts))
        windows[trade.symbol] = (min(low, trade.entry_ts), max(high, trade.exit_ts))

    out: dict[str, list[Bar]] = {}
    for symbol, (first, last) in windows.items():
        try:
            out[symbol] = list(await bar_repo.get_bars(symbol, timeframe, first, last))
        except Exception as exc:
            log.warning(
                "analytics.excursion_bars_unavailable",
                symbol=symbol,
                error=str(exc),
                msg="this symbol's trades report null MAE/MFE",
            )
    return out


@router.get("/attribution")
async def get_attribution(
    order_repo: Annotated[OrderRepository, Depends(get_order_repository)],
    settings: Annotated[Settings, Depends(get_settings)],
    clock: Annotated[Clock, Depends(get_clock)],
    by: str = "strategy",
    start: date | None = None,
    end: date | None = None,
) -> AttributionResponse:
    """P&L grouped by strategy | symbol | weekday | hour | exit_reason.

    An unknown dimension is a 422 naming the ones that exist, rather than an
    empty list. "You asked for something that does not exist" and "this period
    made nothing" are different answers and only one of them should look like a
    quiet period.
    """
    if by not in ATTRIBUTION_DIMENSIONS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"cannot attribute by {by!r}; known dimensions are "
                f"{', '.join(ATTRIBUTION_DIMENSIONS)}"
            ),
        )

    window_start, window_end = _window(start, end, clock.now())
    trades = _in_window(
        await _reconstruct(order_repo, settings, end=window_end, strategy_id=None),
        window_start,
        window_end,
    )
    rows = PerformanceAnalyzer().attribution(trades, by)
    return AttributionResponse(
        by=by,
        rows=[AttributionRowView(**asdict(row)) for row in rows],
        start=window_start,
        end=window_end,
    )


@router.get("/live-vs-backtest/{strategy_id}")
async def live_vs_backtest(strategy_id: str) -> dict[str, object]:
    """Is live performing as the backtest promised?

    The most important report here. Persistent negative divergence means the
    backtest was wrong — overfitting, unmodelled costs, or unachievable fills.

    Still a stub, and deliberately so: this is its own roadmap item (Phase 5,
    "Live-vs-backtest comparison"). The half that belongs to *this* item is
    built — `PerformanceAnalyzer.compare_to_backtest` computes the divergence
    metric by metric — and what is missing is the other operand. There is no
    stored backtest result to compare against: `backtest_runs` has no reader,
    and `/backtests` is a stub too. Answering with a comparison against a
    backtest run on the fly here would compare live against whatever parameters
    this request happened to pass, rather than against the backtest that
    approved the strategy, which is the only comparison worth making.
    """
    raise NotImplementedError


@router.get("/reports/daily")
async def daily_report(day: date | None = None, output_format: str = "json") -> dict[str, object]:
    """End-of-day summary: P&L, trades, rejections, halts, feed incidents.

    Still a stub: its own roadmap item (Phase 5, "Daily report"). Trades and P&L
    are available from this module now, and the other three are not gathered
    anywhere one query can reach — rejections live in the signals table, halts in
    the kill switch's records, feed incidents only in the worker's logs.
    """
    raise NotImplementedError
