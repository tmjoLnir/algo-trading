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

`BarRepository.find_gaps()` must consult the trading calendar. Every weekend and
holiday looks like a gap otherwise, and an alert that fires every Saturday is
ignored by the second week.

Real gaps: vendor outages, our downtime, halted symbols, newly listed tickers.
Never backtest across an unfilled gap — a hole treated as "no movement" produces
a flattering, fictional equity curve. `DataGapError` exists to stop this.

## Real-time pipeline

```
Alpaca WS → StreamIngestor ─┬→ Redis quote cache   (risk checks read this)
                            ├→ bars table           (durable)
                            └→ Redis pub/sub → API WS → dashboard
```

One process owns the upstream connection: Alpaca limits connections per key, and
more importantly gap detection and reconnect logic belong in exactly one place.

**On reconnect, backfill before resuming.** Events during the gap are gone, and
indicators computed across an unfilled hole are wrong in a way nothing
downstream can detect.

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

- [ ] No gaps outside market closures
- [ ] No duplicate `(symbol, timeframe, ts)`
- [ ] OHLC consistent: `low ≤ open,close ≤ high`
- [ ] No zero or negative prices
- [ ] Volume plausible (a 100× spike is usually a bad print, occasionally news)
- [ ] Adjusted and raw both present
- [ ] Timestamps UTC and aligned to the timeframe
