# Market data

Requirement #4. Everything downstream is conditional on this being right: a
backtest over a data gap and a strategy trading on a stale quote both fail
silently, which is the worst way to fail.

## Sources

| | Historical | Real-time |
|---|---|---|
| Provider | Alpaca `/v2/stocks/bars` | Alpaca WebSocket |
| Feed | IEX (free) / SIP (paid) | same |
| Storage | TimescaleDB `bars` | Redis cache + `bars` |

**The free IEX feed is ~2–3% of consolidated volume**, and the free tier
withholds the most recent 15 minutes of SIP data. Build against it, but do not
develop a strategy whose edge lives inside that window and discover this later.

## Storage

`bars` is a TimescaleDB hypertable partitioned on `ts` — the only unbounded
table. Natural key `(symbol, timeframe, ts)`; upserts are idempotent because
backfills overlap constantly.

Store **both** `close` (raw) and `adj_close` (split/dividend adjusted).
Backtest on adjusted, trade on raw. Mixing them produces P&L that cannot be
reconciled against the broker.

## Gaps

`BarRepository.find_gaps()` consults the trading calendar (`atp_core.clock
.TradingCalendar`, backed by `pandas_market_calendars`). Every weekend and
holiday looks like a gap otherwise, and an alert that fires every Saturday is
ignored by the second week.

Real gaps: vendor outages, our downtime, halted symbols, newly listed tickers.
Never backtest across an unfilled gap — a hole treated as "no movement" produces
a flattering, fictional equity curve. `DataGapError` exists to stop this.

```bash
make backfill sym=SPY from=2020-01-01            # fetch
uv run python scripts/backfill_bars.py --symbols SPY --start 2020-01-01 --verify
```

`--verify` re-reads what was stored and reports the sessions that have no bar.
It exits non-zero when it finds any, so a pipeline cannot mistake a partial
dataset for a clean one.

A nightly job (`atp_worker.scheduler.backfill_missing_bars`) runs the same check
unattended over the last 7 days, for every `(symbol, timeframe)` already stored,
and fetches what is missing. It re-checks afterwards and logs a WARNING naming
anything it could not fill — "fetched 3 windows" and "the holes are gone" are
different claims, and a job that cannot tell them apart reports success every
night while the hole stays put. Anything older than the lookback is an operator
job: `--verify` over the range in question.

**What is expected.** One daily bar per session; for intraday, one bar per
interval from the session open for as many whole intervals as fit before the
close — 13 half-hours in a regular session, 7 in a 13:00 early close. A bar is
only expected once the range being checked covers the whole of it, so an
in-progress session is never reported as a hole.

**The daily anchor.** Alpaca stamps a daily bar at 00:00 New York, not at the
session open, so a stored daily bar is matched to a session by the
exchange-local date its timestamp falls in. A feed that stamped daily bars at
00:00 UTC would attribute every bar to the *previous* session — normalise that
in its adapter rather than loosening the rule. `find_gaps` logs a warning when
stored bars land outside every session, which is what that mistake looks like
from the inside.

Confirmed on live data, not just asserted: SPY dailies arrive at 05:00Z in
winter and 04:00Z in summer. It is genuinely 00:00 *New York* and not a fixed
UTC offset that happens to look right for half the year — a distinction worth
keeping, because an offset hard-coded from a January sample misattributes every
bar from March to November.

**`1h` and `4h` are refused.** Neither divides a 390-minute session, and where
the vendor puts the remainder is unverified. A misaligned grid reports every
session as a gap, which is worse than answering "I cannot check this".

**A missing intraday bar is not always missing data.** Alpaca emits no bar for a
minute in which nothing traded, so on an illiquid symbol these are ordinary. The
same is not true of daily bars: a session with no daily bar is a real hole.
Bars at the very end of a range may also simply not have been published yet —
the free tier withholds the most recent 15 minutes.

## Real-time pipeline

```
Alpaca WS → StreamIngestor ─┬→ Redis quote cache   (risk checks read this)
                            ├→ bars table           (durable)
                            └→ Redis pub/sub → API WS → dashboard
```

One process owns the upstream connection: Alpaca limits connections per key, and
more importantly gap detection and reconnect logic belong in exactly one place.
A second process asking for the same key is refused with code 406, and
`AlpacaRealtimeFeed` treats that as permanent rather than retrying — two
ingestors fighting over one connection is worse than one that fails loudly.

**Reconnecting and gap-filling are split, deliberately.** The feed adapter owns
the socket: exponential backoff 1s → 30s with jitter, subscriptions replayed on
the way back up, and it gives up rather than looping forever on an error another
connection would not fix (bad credentials, a plan that does not cover the feed,
the connection limit). The `StreamIngestor` owns the *data* gap, because the
historical provider and the bar store are its dependencies, not the feed's.

**It gives up on a stopwatch, not a counter.** The ladder ran for eight attempts
until day 1 of the paper week, which sounds generous and is not: a doubling
backoff spends most of its budget on the last two waits, so eight attempts
expired about four minutes in. Alpaca was unreachable for roughly seven minutes,
so the stream raised, the worker died, restarted, reset the counter and died
again — three times (docs/paper-week/day-1-review.md, F6). It is now bounded by
`ws.RECONNECT_BUDGET_SECONDS`, fifteen minutes of elapsed time, and the
per-attempt ceiling was halved so a long outage is retried more often rather
than less. "How long can this venue be away before we stop trying" is a question
an operator can answer; "how many attempts is that" was one they had to
integrate by hand.

The window is derived from **the last bar in storage** as well as from the
feed's own gap marker, whichever is earlier. The marker is measured from the
current process's stream start, so a restart mid-outage silently shrinks it —
which is exactly what happened on day 1: the fourth worker believed the gap was
23 seconds rather than eight minutes, asked for a one-minute window, and
succeeded by its own definition. None of `backfill_failed`, `backfill_skipped`
or `backfill_truncated` fired and ~108 bars are permanently absent (F5). The bar
table cannot be reset by a restart, so it is the second opinion; a disagreement
is logged as `data.stream.gap_widened_from_storage`. An over-wide window costs a
few redundant upserts, and a too-narrow one costs the data.

**On reconnect, backfill before resuming.** Events during the gap are gone, and
indicators computed across an unfilled hole are wrong in a way nothing
downstream can detect. The two halves meet at `FeedReconnected`, which the feed
yields *into the event stream* rather than onto a callback: one `async for` body
runs to completion before the next event is delivered, so the backfill provably
finishes before anything from the new connection is handled. A callback could
not promise that ordering, and the ordering is the whole requirement.

The re-fetch window is `[last message, last completed bar)`, both ends snapped
to the bar grid. The start, because a drop at 10:30:45 lost part of the 10:30
bar and the socket will never re-send it. The end, because the bar in progress
is not missing — we are subscribed again before it closes and the feed will
deliver it whole, so pulling a partial one from REST would be a downgrade. An
outage that opens and closes inside one bar therefore costs nothing: a blip is
not a data incident.

Bars fetched this way are **raw, not adjusted**. That halves the requests, and
raw is what the live path compares against anyway; the nightly sweep re-fetches
the same range adjusted, so nothing stays raw-only.

One reconnect chases at most `MAX_RECONNECT_BACKFILL` of history. A
`last_message_at` from three days ago means the process was down, and turning
that into a three-day minute backfill would block the live stream behind
thousands of requests at exactly the moment it recovered. What is dropped is
named in the log and swept up by the nightly job.

**A gap that cannot be closed halts trading.** Not by killing the ingestor —
quotes and bars keep flowing to the cache and the table, and taking the
dashboard down buys nothing — but by engaging the kill switch. The stream is
healthy; the history has a known hole in it, and trading across that hole is the
exact failure this pipeline is built to prevent.

## Quote cache and fan-out

`RedisQuoteCache` holds one key per symbol — `atp:md:quote:<SYMBOL>` — containing
a small JSON document with every number rendered as a **string**. JSON has one
numeric type and it is a binary float, so a price stored as a JSON number comes
back subtly wrong and comes back silently. Reads are a single `GET`, or one
`MGET` for a whole watchlist, because this is read on every risk check.

**The TTL is garbage collection, not freshness.** It is seven days — long enough
to span a three-day weekend plus a holiday. Freshness is judged from the `ts`
inside the payload, by `StaleDataRule`. If expiry were the freshness mechanism
then a dead feed would turn into `get_quote() -> None`, and "I have no quote for
AAPL" and "my AAPL quote is four hours old" would become the same answer. They
are not: the second one means something is broken and has to say so.

`RedisEventPublisher` is the pub/sub leg. It refuses to publish a Python float —
the last place a price can be checked before it leaves the process. Redis pub/sub
has no persistence and no delivery guarantee, which is the right trade for tick
traffic and the wrong one for anything that must not be lost: a fill, a halt or a
position change goes to the database first and onto a channel second.

## Staleness

**Staleness is not silence.** A frozen feed looks identical to a quiet market
from the inside. `StalenessMonitor` is calendar-aware — silence at 02:00 Sunday
is correct, the same silence at 14:30 Tuesday means something is broken.
`StaleDataRule` refuses to trade on a quote older than 30s.

It is the only thing that catches a feed that is *connected and frozen*. A
dropped socket is the feed adapter's problem and it reconnects; a socket that
stays open and stops delivering looks healthy from every other vantage point.

Silence is measured from the **latest** of three instants, and each one stops a
specific false alarm:

| Instant | Without it |
|---|---|
| last message received | — the obvious baseline |
| `connected_since` | a worker started at 11:00 is accused of missing the 09:30 open it was never running for |
| the session open | a feed that died at yesterday's close registers as silent for eighteen hours the moment the bell rings |

Take the earliest instead and the watchdog fires on every restart and every
morning. Take only the last message and it cannot speak before the day's first
tick — exactly when a broken feed most needs reporting.

It halts once per outage and **never clears**. Engaging is reflexive, clearing is
deliberate and needs a named human (docs/SAFETY.md). A watchdog that un-halted
itself would let a feed flapping every thirty seconds trade through every gap.

## Corporate actions

Splits and dividends change historical prices and share counts. Applied
pre-open by a scheduled job.

An unapplied 4:1 split makes a position look like it lost 75% overnight — which
will trip stops and the daily loss limit on a day when nothing happened. A
reverse split is the same defect with the sign flipped and is far worse,
because it reads as a profit: GE's 1:8 on 2021-08-02 octupled its raw price
overnight.

**Backtests do not rely on that job.** `BacktestEngine.run` converts every bar
into adjusted space itself, scaling the whole candle by `adj_close / close` and
volume by its inverse, so a replay is continuous across an action whenever it
was run. Bars with no `adj_close` refuse the run rather than being priced raw —
see `docs/adr/0017-backtests-price-off-adjusted-closes.md`. This is why the
`--raw-only` backfill is a realtime-path convenience and not a way to save a
pass when filling history you intend to backtest.

## Backfill

```bash
make backfill sym=AAPL,MSFT,SPY from=2020-01-01
```

Rate limit is 200 req/min on the free tier. The script batches symbols, paginates
via `next_page_token` to exhaustion, and backs off on 429.

## Seeded data, and why it is not in this table

`make seed` writes bars too, and they are **fabricated** — a driftless random
walk from `atp_core.data.seed`, so a fresh clone with no vendor credentials still
has something to run a backtest against.

They are written only under NASDAQ's reserved test tickers (`ZVZZT`, `ZWZZT`,
`ZXZZT`, `ZJZZT`), and that namespace is the whole safety argument rather than a
convention. Upserts here are keyed on `(symbol, timeframe, ts)`, so a fabricated
`SPY` would not sit beside a real `SPY` history — it would overwrite it, bar for
bar, with no error and no trace. `require_reserved` refuses any other symbol and
takes no override.

Nothing about a seeded series is evidence. It has no exploitable structure by
construction, so a strategy scoring well on one has found noise; the reference
run over the default window returns −1.0% with a Sharpe of −0.12 after realistic
costs, which is what "nothing to find" looks like. Real history comes from
`backfill_bars.py` and nowhere else.

## Sanity checks before trusting a dataset

- [ ] No gaps outside market closures — `backfill_bars.py --verify`
- [ ] No duplicate `(symbol, timeframe, ts)`
- [ ] OHLC consistent: `low ≤ open,close ≤ high`
- [ ] No zero or negative prices
- [ ] Volume plausible (a 100× spike is usually a bad print, occasionally news)
- [ ] Adjusted and raw both present
- [ ] Timestamps UTC and aligned to the timeframe
