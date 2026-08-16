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
the socket: exponential backoff 1s → 60s with jitter, subscriptions replayed on
the way back up, and it gives up rather than looping forever on an error another
connection would not fix (bad credentials, a plan that does not cover the feed,
the connection limit). The `StreamIngestor` owns the *data* gap, because the
historical provider and the bar store are its dependencies, not the feed's.

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

**Staleness is not silence.** A frozen feed looks identical to a quiet market
from the inside. `StalenessMonitor` is calendar-aware — silence at 02:00 Sunday
is correct, the same silence at 14:30 Tuesday means something is broken.
`StaleDataRule` refuses to trade on a quote older than 30s.

## Corporate actions

Splits and dividends change historical prices and share counts. Applied
pre-open by a scheduled job.

An unapplied 4:1 split makes a position look like it lost 75% overnight — which
will trip stops and the daily loss limit on a day when nothing happened.

## Backfill

```bash
make backfill sym=AAPL,MSFT,SPY from=2020-01-01
```

Rate limit is 200 req/min on the free tier. The script batches symbols, paginates
via `next_page_token` to exhaustion, and backs off on 429.

## Sanity checks before trusting a dataset

- [ ] No gaps outside market closures — `backfill_bars.py --verify`
- [ ] No duplicate `(symbol, timeframe, ts)`
- [ ] OHLC consistent: `low ≤ open,close ≤ high`
- [ ] No zero or negative prices
- [ ] Volume plausible (a 100× spike is usually a bad print, occasionally news)
- [ ] Adjusted and raw both present
- [ ] Timestamps UTC and aligned to the timeframe
