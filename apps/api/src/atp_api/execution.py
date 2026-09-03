"""The API's end of the single submit path.

An operator closing a position from the dashboard is placing an order, and ADR
0005 is explicit that there is one way to do that: build an `OrderRequest` and
hand it to `OrderRouter.submit()`, which calls `RiskEngine.validate()` before
anything reaches a venue. "Manual orders in particular are the most common
reason to need limits, not a reason to skip them."

So this module assembles that path inside the API process. It is deliberately
small and deliberately explicit about the two things that make the API's copy
different from the worker's:

**The book is read, not held.** The worker owns the live `Portfolio`; ADR 0007
says the worker publishes the book and everyone else reads it. The API's copy
comes from `PortfolioRepository.latest`, which is the last snapshot the runner
wrote — up to one evaluation old. That is fine for deciding *how much* to close
because the router re-derives the quantity from the position it is handed, and
it is why `flatten` is the only thing built on this path: an entry sized off a
stale book would be sized wrong, while an exit off a stale book closes a
quantity the venue will simply cap at what is actually there.

**The feed clock comes from the quote cache.** `StaleDataRule` needs
`last_tick_at`, which in the worker is the ingestor's in-memory record of when
each symbol last ticked. There is no ingestor here, so the same question is
answered from the quotes this module has just read — the same Redis the
ingestor writes to, one hop later. A symbol with no cached quote reads as
"never seen", which denies the order, which is the correct answer: refusing to
trade a symbol whose price we cannot establish is what the rule is for.

What is **not** here is the emergency flatten. `POST /api/v1/risk/flatten-all`
calls `BrokerPort.close_all_positions()` directly, which is the one carve-out
ADR 0005 names and defends: it is a human acting around a platform they have
already lost confidence in, and therefore around a book they cannot build a
correct `OrderRequest` from.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from atp_core.execution.router import OrderRouter
from atp_core.risk.engine import RiskEngine, default_rules
from atp_core.risk.stops import StopManager

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

    from atp_core.brokers.ports import BrokerPort
    from atp_core.clock import Clock, TradingCalendar
    from atp_core.data.ports import QuoteCache
    from atp_core.domain import Portfolio, Quote, RunMode
    from atp_core.execution.ports import PortfolioRepository
    from atp_core.risk.killswitch import KillSwitch
    from atp_core.risk.limits import RiskLimits


def build_router(
    *,
    broker: BrokerPort,
    kill_switch: KillSwitch,
    clock: Clock,
    calendar: TradingCalendar,
    limits: RiskLimits,
    quotes: Mapping[str, Quote],
) -> OrderRouter:
    """One router for one request, over the full nine-rule chain.

    Per-request rather than per-process, and the reason is `quotes`: the chain
    closes over the freshness of the book it was built for, so a router cached
    across requests would answer `StaleDataRule` with whatever prices the first
    caller happened to see. Building one costs no I/O — every collaborator here
    is already constructed — so there is nothing to amortise.

    `default_rules` rather than a subset. Two of the nine cannot mean here what
    they mean in the runner and are left in anyway rather than dropped:
    `DailyLossLimitRule` is unanchored in this process, and `RateLimitRule`
    counts only what this router has seen, which is one order. Neither is
    theatre for the path this serves — an exit bypasses the loss limit by
    design (a rule that blocks an exit traps you in a losing position), and a
    single operator action cannot run away. Dropping them would mean shipping a
    chain that differs from the documented one, which is the harder thing to
    reason about later.

    A fresh `StopManager` for the same reason it is fresh in every process: it
    holds engine-side levels for positions this router did not open, and the
    ones that matter are broker-side anyway. `flatten` cancels protection
    through the venue, not through this object.

    `limits` arrives as a value rather than being read off `Settings`, because
    the ceilings are a stored row now and reading them is I/O — `get_risk_limits`
    does it once per request as a dependency, which keeps this function what it
    has always been: assembly over collaborators that are already built.
    """

    def last_tick_at(symbol: str) -> datetime | None:
        quote = quotes.get(symbol)
        return quote.ts if quote is not None else None

    risk_engine = RiskEngine(limits, default_rules(kill_switch, clock, calendar, last_tick_at))
    return OrderRouter(broker, risk_engine, StopManager(), clock, kill_switch=kill_switch)


async def marked_book(
    portfolio_repo: PortfolioRepository,
    quote_cache: QuoteCache,
    run_mode: RunMode,
) -> tuple[Portfolio | None, dict[str, Quote]]:
    """The stored book, priced off the quote cache.

    Returns the portfolio and the quotes it was marked with, because the caller
    needs both: the router wants the book, and `build_router` wants the same
    quotes so that what valued the position and what judged its freshness are
    one reading rather than two.

    `None` means no snapshot has ever been written — a platform that has not
    traded. It is not an error and it is not an empty book: the caller must say
    "there is no book" rather than "you hold nothing".

    A position whose symbol has no cached quote is left **unmarked**, on
    purpose. `Portfolio.equity` treats an unmarked holding as worthless, so
    every percentage limit computed from it comes out too small and approves
    what it should refuse — which is why `_unpriced_book` refuses outright, by
    name, listing the symbols it could not price. Filling the hole with a last
    bar close, as the runner does, would need a bar repository and would be
    substituting a stale number for a missing one at exactly the moment the
    difference matters.
    """
    portfolio = await portfolio_repo.latest(run_mode)
    if portfolio is None:
        return None, {}

    open_symbols = [position.symbol for position in portfolio.open_positions]
    if not open_symbols:
        return portfolio, {}

    quotes = await quote_cache.get_quotes(open_symbols)
    for symbol, quote in quotes.items():
        portfolio.position(symbol).last_price = quote.mid
    return portfolio, quotes
