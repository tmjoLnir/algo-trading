# Risk management

Requirement #3. Risk management is not a feature of this platform — it is the
platform. A trading system without it is a fast way to lose money.

---

## Three layers

1. **Position sizing** — decides how much, before the trade.
2. **Stop losses** — bound the loss on a trade that goes wrong.
3. **Portfolio limits** — bound the damage when several go wrong at once.

All three are needed. Perfect sizing does not survive a gap; perfect stops do
not survive twenty correlated positions.

## Position sizing

Configured per strategy in `RuleSet.risk.position_size`.

| Method | Formula | Use when |
|---|---|---|
| `fixed_qty` | n shares | testing only |
| `fixed_notional` | $X / price | rarely — ignores volatility |
| `equity_pct` | equity × pct / price | simple, volatility-blind |
| **`risk_pct`** | **equity × pct / \|entry − stop\|** | **default** |
| `volatility_target` | scale to a target portfolio vol | multi-asset books |

### Why `risk_pct` is the default

It equalises *risk*, not *notional*.

> Equity $100,000, risk 1% = $1,000 per trade.
> Stock A at $50, stop at $48 → risk $2/share → **500 shares** ($25,000).
> Stock B at $50, stop at $35 → risk $15/share → **66 shares** ($3,300).

Both lose $1,000 if stopped. Under fixed notional, B — the far more volatile
name — would get the same $25,000 and lose $7,500 on the same stop. Sizing by
notional makes your riskiest positions your largest, which is precisely backwards.

Rule of thumb: 0.5–2% risk per trade. Above 2%, a normal losing streak of 8–10
trades — which happens to good strategies — is an account-threatening event.

**`risk_pct` and the position cap can contradict each other, and the cap wins.**
A wide stop buys a large position by construction: 1% of $100,000 against a
2×ATR stop of $3.27 is 305 shares of a $97 stock, which is 29.5% of the account
against a 10% `max_position_pct` — so `max_position_size` refuses the whole
entry rather than trimming it. Nothing is wrong with either number; they are
measuring different things, and a strategy whose stop is wide relative to its
price needs a risk fraction small enough that the resulting notional fits. This
is visible the moment stops reach a backtest, as a run with a refusal per entry.

### Where it is applied

Two callers, one function. `OrderRouter._size` sizes a live signal and
`backtest.engine.RiskBasedSizer` sizes a backtested one, and both delegate to
`position_size` rather than doing arithmetic of their own — a backtest that
sized differently would report a return the live strategy could not reproduce,
which CLAUDE.md §5 names as the hardest class of bug here to notice.

Both also treat the two inputs `position_size` refuses to default — a stop for
`risk_pct`, a volatility for `volatility_target` — as a **refusal** rather than
an error: one strategy misconfigured, recorded and visible, instead of an
exception that takes a runner's loop or a whole backtest down.

## Stop losses

| Type | Level | Good for |
|---|---|---|
| `fixed_pct` | entry × (1 − x) | simple, predictable |
| `fixed_amount` | entry − $x | a level you have a specific reason for |
| `atr` | entry − n × ATR | **default** — adapts to volatility |
| `trailing_pct` | high-water × (1 − x) | trend following |
| `chandelier` | highest-high − n × ATR | trends, volatility-adjusted |
| `time` | exit after n bars | mean reversion that has not reverted |

**ATR-based stops are the default.** A fixed 2% stop is far too tight on a
volatile small-cap — you are stopped out by ordinary noise — and far too loose
on a utility. ATR scales with what the instrument actually does. Typical: 2–3 ×
ATR(14).

### Placement rules

- **Broker-side stops for live positions.** A stop that only exists in our
  process protects nothing when the process is dead. Engine-side logic layers on
  top to tighten it.
- **Never widen a stop.** Trailing stops must be monotonic. Moving a stop away
  from price to avoid being hit converts a planned small loss into an unplanned
  large one, and it always feels justified at the time.
- **Track the high-water mark off bar highs, not closes** — an intraday spike
  that should have ratcheted the stop is invisible in closes.
- **Place protective orders immediately on entry fill.** The gap between owning
  a position and having a stop is unprotected exposure.
- **The stop goes to the venue; the take-profit does not.** `OrderRouter
  .submit_protective_orders` sends a broker-side stop and arms
  `position.take_profit_price` for the engine to watch. The asymmetry is
  deliberate: `BrokerPort` has no bracket, so a stop and a target for the same
  shares are two independent live orders, and when one fills the other is still
  working — filling it would not close anything, it would open a fresh position
  on the opposite side. A target that only exists in our process is an
  acceptable loss when the process dies; a stop is not, which is why SAFETY.md
  has a layer for one and not the other.
- **An entry that fills in pieces gets a stop per piece.** Protection is
  additive rather than cancel-and-replace: replacing opens an unprotected window
  between the cancel landing and the replacement being acknowledged, and the
  cancel can lose the race outright.
- **A protective stop can be refused.** Four of the nine rules judge the order
  rather than whether it reduces a position, so the kill switch, trading hours,
  the rate limit and stale data can each block one; two more block it whenever
  another holding is unmarked. Only the daily loss limit, buying power and the
  open-position cap can never refuse one. The router reports a refusal as an
  unprotected quantity and logs `CRITICAL` rather than exempting the order —
  see docs/RUNBOOK.md, "Position open with no stop".
- **A stop the market has already passed is not placed.** Submitted, it is a
  market order in disguise; armed, it triggers on the next bar. The reachable
  case is a reversal that has only partly filled — the position is still on the
  old side while the level belongs to the side being opened.
- **A flip through zero invalidates the old side's stops.** `apply_fill` clears
  protective levels only at exactly flat, and a flip never passes through flat,
  so a sell stop under what is now a short survives and *adds* to the short when
  it triggers. The router cancels them before placing the new side's.
- **Protective levels are cleared when a position goes flat.** `apply_fill`
  does this, and it matters: a stop left armed across a flat would reference a
  basis that no longer exists, and would arm itself against whatever position
  opens next in that symbol — a live order at a price that means nothing to it.
- **A stop below zero is not a stop.** A percentage over 1, or an ATR multiple
  wide enough to swamp the entry, produces a level price can never reach. The
  position looks guarded and is not, so `StopManager` refuses it at
  construction rather than arming it.

### Where they are applied

Two callers, one `StopManager` — the same shape sizing has, and for the same
reason. `StrategyRunner` derives a level for a live signal and
`BacktestEngine` derives one for a backtested signal; both call `initial_stop`,
`update_trailing`, `time_exit_due`, `should_trigger` and `take_profit_level`
rather than comparing prices themselves.

Until a backtest could be given a `StopConfig`, only the live half of that
existed. A strategy configured behind an ATR stop was backtested
naked, because no shipped strategy emits a level of its own — so the backtest
measured a strategy nobody was going to run. `--stop` on the CLI, the **Stop**
field on the Backtests tab, and `stop_type` on a `BacktestRunSpec` are the same
choice reaching a replay; docs/BACKTESTING.md has the table and the caveats.

Two differences a replay cannot avoid, both deliberate:

- **Nothing is broker-side.** There is no venue in a replay, so `broker_side` is
  False on every backtest stop. A config claiming otherwise would describe
  protection the run does not provide, and the first placement rule above would
  read as satisfied when it was not.
- **A bar is the finest resolution there is.** When a bar's range spans both the
  stop and the target, the stop is taken. The bar cannot say which came first,
  and the pessimistic reading is the only honest one.

Everything else holds identically, including the two anchors a derived level
uses: the stop comes from the price the decision was taken at, because that is
what the sizer measured risk against, and the take-profit comes from the actual
fill. `OrderRouter.submit_protective_orders` does exactly the same, and if that
ever changes it has to change in both places or a backtest and a live run will
arm different levels from the same signal.

### What stops cannot do

A stop is not a guarantee of price. A stock closing at $50 and opening at $32 on
an earnings miss fills your $48 stop at $32. Overnight gap risk is real, and the
only defences are position size and `flatten_at_close` — not tighter stops.

## Portfolio limits

Hard ceilings, enforced by `RiskEngine` on every order:

| Limit | Default | Prevents |
|---|---|---|
| `max_position_pct` | 10% | one position becoming the book |
| `max_gross_exposure_pct` | 100% | unintended leverage |
| `max_daily_loss_pct` | 3% | a bad day compounding into a disaster |
| `max_orders_per_minute` | 30 | a runaway loop |
| `max_open_positions` | 20 | unmonitorable sprawl |

**Where they live.** These were `RISK_*` environment variables until ADR 0025.
They are now columns on the `worker_config` row, edited in the risk section of
the dashboard's **Config** tab, and every save is one audit entry naming the
operator and both numbers. The values above are the defaults an unconfigured
deployment runs on — the same ones `.env.example` used to ship.

**Two processes read them at different moments, and the screen says which.** The
worker builds its `RiskEngine` once at start, so a tightened ceiling binds it at
its next restart; the API builds a router per request, so an order you place
from the dashboard is measured against the row as saved, immediately. The
settings screen shows the saved and the running numbers side by side and says
when a restart is owed.

**Bounds are enforced at the point of typing.** A fraction must be above zero
and no wider than its ceiling — 100% of equity for a single position, Reg-T's
400% for gross exposure, and strictly *below* a whole entry price for the
default stop, which at exactly 1 would be a stop at zero. Zero is refused
everywhere rather than read as "off": a zero position limit refuses every order
and a zero daily-loss limit halts on the first cent, and an operator who wants
trading stopped has a kill switch that says so. `max_position_pct` may not
exceed `max_gross_exposure_pct` — the gross rule would refuse first, so the
position limit you typed would not be the one in force.

Gross, not net: a long/short book that nets to zero still borrows and still
loses on both legs in a correlated shock.

**The daily loss limit blocks entries, never exits.** Refusing to let a losing
position close would turn a bad day into an unbounded one.

### Orders in flight count against every limit above

A limit is checked per order, and the book only moves when a fill lands. So a
strategy that emits many orders at once — which is what any multi-symbol
universe does — used to have each one judged against a book containing none of
the others. Forty entries at 5% of equity each pass a 100% gross cap
individually and land at 200% together.

That was not hypothetical. A 40-symbol `buy_and_hold` replay filled all forty,
ended at **1.97x gross exposure with cash at −97,046**, and the exposure cap
refused nothing. All four limits that describe the shape of the book have the
same hole: `max_open_positions` counts positions, so a batch submitted at
nineteen open all passes; `max_position_pct` reads one symbol's quantity, so two
orders in the same name at 6% each pass a 10% cap.

`RiskEngine.validate` therefore takes what the caller has already committed and
not yet seen settle, and projects the book forward before running the chain
(`rules.project_pending`). `BacktestEngine` passes its resting orders;
`StrategyRunner` passes what it believes is working at the venue. **A caller
that has orders outstanding and does not pass them gets the old behaviour**, so
this is a parameter with exactly two correct call sites, not an option.

Two things the projection deliberately does *not* do:

- **It never credits a reduction.** A resting protective stop, counted as
  filled, would lower projected exposure and license a position the limits would
  otherwise refuse — a rule reasoning from an exit that has not happened.
- **It does not make a limit an invariant.** The chain prices an in-flight order
  at the last mark and the fill then crosses the spread, so a book can settle a
  hair over a ceiling it was approved under. That residual is slippage-scale
  (tens of dollars on a 100,000 account), is the same for a single order, and is
  inherent to any pre-trade check.

### The correlation trap

Ten positions at 5% each is not 50% diversified exposure if all ten are regional
banks. In a sector shock they are one position at 50%. Per-position limits do
not see this; a sector or factor exposure limit is the fix, and it is on the
roadmap rather than in the skeleton.

## The kill switch

Halts everything. Engaging needs no confirmation — hesitation is the expensive
part. Clearing requires a named human and is audit-logged.

Auto-engages on: daily loss limit breach, reconciliation mismatch, data feed
loss, broker unreachable, a rate-limit storm, repeated unhandled exceptions.

**Fails closed.** The switch lives in Redis so that the API can trip it while
the worker is mid-loop, and so that it survives a restart — a switch that
cleared on restart would let a crash loop silently resume trading. If Redis
cannot be reached the switch reports *engaged*: a false halt costs missed
opportunity, a false clear trades the account through whatever broke Redis.

**Halting is not flattening.** Halting stops new risk. Flattening realises
existing P&L and is not always right — a data outage means stop trading, not
dump the book into a market you cannot currently see.

## Position accounting

`Position.apply_fill()` has three cases, and getting them wrong corrupts every
P&L number downstream:

1. **Open or add** — re-average the cost basis. No P&L realised.
2. **Reduce** — realise P&L against the existing basis. **Basis unchanged.**
3. **Flip through zero** — close the old side fully (realising its P&L), then
   open the new side at the fill price.

Case 2 is the one people get wrong: re-averaging on a reduction quietly
misstates every subsequent trade on that symbol.
