"""Synthetic bars for a development database.

`scripts/seed.py` needs bar history, and a fresh clone has no vendor credentials
to fetch real history with. So this fabricates it — and every decision in this
module is about making sure what it fabricates cannot be mistaken for evidence.

Two rules do that work, and neither is a label that can be overlooked:

- **Reserved symbols only.** Bars are written under NASDAQ's test tickers
  (`ZVZZT` and its siblings), which the industry reserves for exactly this
  purpose and which no backfill will ever fetch. That is a namespace, not a
  disclaimer: a fabricated series cannot land on top of a real `SPY` history and
  overwrite it — `upsert_bars` is keyed on `(symbol, timeframe, ts)` and would
  do precisely that — and a report headed `ZVZZT` is not one anybody promotes a
  strategy on. `require_reserved` is the guard, and it has no override.
- **No exploitable structure.** The series is a driftless geometric random walk.
  There is nothing in one for a strategy to find, by construction, so a seeded
  backtest that looks profitable is noise — which is the correct thing for it to
  be. docs/BACKTESTING.md's argument throughout is that a believable number from
  a source that cannot support it is worse than no number at all.

**Deterministic.** The same symbol over the same window produces the same bars
on every machine and on every re-run, seeded from those arguments and from
nothing else — no clock, no entropy. A development database whose contents
shifted under a re-seed would make every backtest run against it
unreproducible, and reproducibility is the one property a backtest cannot do
without (docs/BACKTESTING.md, and `Strategy`'s own determinism contract).

Pure: no I/O, no network, no clock (CLAUDE.md §1.3). It lives here rather than
inside the script so that the shape of the data can be tested without standing
up a database.
"""

from __future__ import annotations

import hashlib
import math
import random
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Final

from atp_core.domain import Bar, Timeframe
from atp_core.errors import ConfigError

if TYPE_CHECKING:
    from collections.abc import Iterable

    from atp_core.clock import TradingCalendar

#: The only symbols this module will fabricate bars for.
#:
#: These are NASDAQ's real test securities — they exist, they carry no value,
#: and no data vendor serves a price history for them. Borrowing the venue's own
#: reserved namespace rather than inventing one (`FAKE1`, `SEED.A`) means the
#: symbols cannot collide with a ticker that is real today or becomes real next
#: year, which is the failure that would put fabricated bars into a series
#: somebody later backtests for real.
RESERVED_TEST_SYMBOLS: Final[frozenset[str]] = frozenset({"ZJZZT", "ZVZZT", "ZWZZT", "ZXZZT"})

#: What a seed writes when nobody names symbols. Three rather than one, so a
#: multi-symbol backtest — where the engine merges timelines across symbols — is
#: exercisable against a seeded database.
DEFAULT_SEED_SYMBOLS: Final[tuple[str, ...]] = ("ZVZZT", "ZWZZT", "ZXZZT")

#: The window a seed covers when nobody names one — three years of sessions,
#: which is roughly 750 daily bars.
#:
#: **Fixed dates rather than "the last three years", and that is the point.**
#: A window anchored to today would move, and because the series is generated
#: from its own start date, moving it would silently change every bar a
#: developer had already backtested against. Seeded data has no reason to be
#: recent — it is not real, and a run dated 2024 is a useful reminder of that —
#: whereas it has every reason to be the same on every machine and in every
#: month. Three years rather than the one this script's stub proposed: an
#: SmaCrossover(20, 50) spends 51 bars on warmup, and a single year leaves too
#: few round trips behind to exercise the metrics that read them.
DEFAULT_FIRST_DAY: Final[date] = date(2022, 1, 1)
DEFAULT_LAST_DAY: Final[date] = date(2024, 12, 31)

#: Annualised volatility of the generated series. Roughly a broad equity index:
#: high enough that an SMA crossover actually crosses, low enough that the
#: result is not dominated by a handful of enormous days.
DEFAULT_ANNUAL_VOLATILITY: Final[float] = 0.18

#: Where a series starts. A round number, because every property of a seeded run
#: is a property of this number and pretending otherwise by picking 137.42 would
#: only make it look researched.
DEFAULT_START_PRICE: Final[Decimal] = Decimal("100.00")

#: Trading days in a year — the constant the daily volatility is scaled by. The
#: same figure `metrics.periods_per_year_for` uses for daily bars.
_TRADING_DAYS_PER_YEAR: Final[int] = 252

#: Shares a typical seeded session turns over. Deliberately large relative to any
#: quantity a developer will backtest: the engine caps a fill at
#: `max_volume_participation` (10%) of the bar's volume, and a thin synthetic
#: series would make every fill partial — teaching a reader something about this
#: module's constants rather than about their strategy.
_BASE_VOLUME: Final[int] = 1_000_000

_CENT: Final[Decimal] = Decimal("0.01")


def require_reserved(symbols: Iterable[str]) -> None:
    """Refuse to fabricate bars for anything but a reserved test ticker.

    The guardrail this module exists behind, and it takes no override flag. The
    accident it prevents is not a reader being fooled by a chart — it is
    `upsert_bars` overwriting a real, expensively backfilled `SPY` history with
    a random walk, silently, because the row key matches. Real history comes
    from `scripts/backfill_bars.py`; there is no legitimate reason to want a
    fabricated `SPY`.
    """
    unreserved = sorted({symbol for symbol in symbols if symbol not in RESERVED_TEST_SYMBOLS})
    if unreserved:
        allowed = ", ".join(sorted(RESERVED_TEST_SYMBOLS))
        raise ConfigError(
            f"refusing to fabricate bars for {', '.join(unreserved)}: seeded data is written "
            f"only under reserved test tickers ({allowed}), so it can never overwrite a real "
            "backfilled history or be mistaken for one. For real bars use "
            "scripts/backfill_bars.py."
        )


def synthetic_daily_bars(
    symbol: str,
    first: date,
    last: date,
    *,
    calendar: TradingCalendar,
    start_price: Decimal = DEFAULT_START_PRICE,
    annual_volatility: float = DEFAULT_ANNUAL_VOLATILITY,
) -> list[Bar]:
    """One daily bar per real trading session in `[first, last]`.

    Sessions come from `TradingCalendar`, not from a weekday filter, so the
    series has the holidays and the early closes a real one has. That matters
    beyond realism: gap detection is calendar-aware, and a seed that emitted a
    bar on Thanksgiving would make `find_gaps` report the seeded data as broken
    in a way no real dataset ever is.

    Each bar is stamped at the session's **exchange-local midnight**, which is
    where Alpaca stamps a daily bar and therefore how `data.gaps` attributes one
    to a session (docs/DATA.md). Stamping at the open instead would place every
    bar in the right day by eye and the wrong one by the code that reads it.

    Prices are `Decimal` at cent resolution (CLAUDE.md §1.1); the random walk
    underneath is float, which is fine — it is generating a number, not tracking
    a balance.
    """
    require_reserved([symbol])
    if start_price <= 0:
        raise ConfigError(f"start_price must be positive, got {start_price}")
    if annual_volatility <= 0:
        raise ConfigError(f"annual_volatility must be positive, got {annual_volatility}")

    rng = _rng_for(symbol, first)
    sigma = annual_volatility / math.sqrt(_TRADING_DAYS_PER_YEAR)
    close = float(start_price)

    bars: list[Bar] = []
    for session in calendar.sessions(first, last):
        previous_close = close

        # The `-σ²/2` is what makes the *price* driftless rather than the log
        # price. Without it a lognormal walk drifts upward at σ²/2 a day, which
        # over a year at 18% vol is a free ~1.6% that a long-only strategy would
        # collect for existing — a small number that is nevertheless exactly the
        # kind of structure this series is supposed not to contain.
        close = previous_close * math.exp(rng.gauss(-0.5 * sigma * sigma, sigma))

        # An overnight gap, then a range around the two ends. Both are scaled by
        # the day's own volatility, so a quiet series stays quiet and the candle
        # shapes do not drift away from the returns they sit on.
        open_ = previous_close * math.exp(rng.gauss(0.0, sigma * 0.3))
        reach = (abs(close - open_) + close * sigma * abs(rng.gauss(0.0, 1.0))) * 0.5
        high = max(open_, close) + reach * rng.random()
        low = min(open_, close) - reach * rng.random()

        opened, closed = _cents(open_), _cents(close)
        # Consistency by construction, not by trusting that the arithmetic above
        # kept its ordering through the rounding and never drew a low at or
        # below zero. `Bar` rejects an inconsistent candle, and a seed that
        # raised on one draw in ten thousand would be a bug nobody could
        # reproduce from the traceback.
        candidates = (opened, closed, _cents(high), max(_cents(low), _CENT))
        ts, _ = calendar.day_bounds(session.day)

        bars.append(
            Bar(
                symbol=symbol,
                ts=ts,
                timeframe=Timeframe.D1,
                open=opened,
                high=max(candidates),
                low=min(candidates),
                close=closed,
                volume=Decimal(int(rng.uniform(0.6, 1.6) * _BASE_VOLUME)),
                # A fabricated series has no corporate actions, so the adjusted
                # close simply *is* the close. Written rather than left null so
                # the column holds what an `--adjusted` backfill would put there
                # — a null would leave a seeded row shaped unlike a real one on
                # the field CLAUDE.md §5 is about.
                adj_close=closed,
            )
        )

    return bars


def _rng_for(symbol: str, first: date) -> random.Random:
    """A generator seeded from the symbol and the start of the window only.

    **`last` is deliberately not part of the seed.** Draws are consumed one
    session at a time from `first` forward, so a longer window appends bars and
    leaves every earlier one byte-identical — re-seeding a wider range extends
    the history instead of rewriting it, which is how a real dataset behaves
    when you backfill more of it. Folding `last` in would reshuffle the entire
    series each time somebody moved the end date by a day, and every backtest
    number taken before that would quietly stop reproducing.

    `random.Random("ZVZZT:…")` would also be deterministic, but by way of a
    documented detail of how `random.seed` handles a string under version 2.
    Hashing states what the seed is a function of and keeps it a function of
    that alone — which is the property being relied on, so it is worth two lines
    to make explicit rather than inherited.
    """
    key = f"{symbol}:{first.isoformat()}".encode()
    return random.Random(int.from_bytes(hashlib.sha256(key).digest(), "big"))


def _cents(value: float) -> Decimal:
    """A float price as an exact Decimal at cent resolution.

    Via `str`, because `Decimal(0.1)` is the binary float's true value to fifty
    digits and quantizing that is a rounding of the wrong number.
    """
    return Decimal(str(value)).quantize(_CENT, rounding=ROUND_HALF_UP)
