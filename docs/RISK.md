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
- **Protective levels are cleared when a position goes flat.** `apply_fill`
  does this, and it matters: a stop left armed across a flat would reference a
  basis that no longer exists, and would arm itself against whatever position
  opens next in that symbol — a live order at a price that means nothing to it.
- **A stop below zero is not a stop.** A percentage over 1, or an ATR multiple
  wide enough to swamp the entry, produces a level price can never reach. The
  position looks guarded and is not, so `StopManager` refuses it at
  construction rather than arming it.

### What stops cannot do

A stop is not a guarantee of price. A stock closing at $50 and opening at $32 on
an earnings miss fills your $48 stop at $32. Overnight gap risk is real, and the
only defences are position size and `flatten_at_close` — not tighter stops.

## Portfolio limits

Hard ceilings in `.env`, enforced by `RiskEngine` on every order:

| Limit | Default | Prevents |
|---|---|---|
| `max_position_pct` | 10% | one position becoming the book |
| `max_gross_exposure_pct` | 100% | unintended leverage |
| `max_daily_loss_pct` | 3% | a bad day compounding into a disaster |
| `max_orders_per_minute` | 30 | a runaway loop |
| `max_open_positions` | 20 | unmonitorable sprawl |

Gross, not net: a long/short book that nets to zero still borrows and still
loses on both legs in a correlated shock.

**The daily loss limit blocks entries, never exits.** Refusing to let a losing
position close would turn a bad day into an unbounded one.

### The correlation trap

Ten positions at 5% each is not 50% diversified exposure if all ten are regional
banks. In a sector shock they are one position at 50%. Per-position limits do
not see this; a sector or factor exposure limit is the fix, and it is on the
roadmap rather than in the skeleton.

## The kill switch

Halts everything. Engaging needs no confirmation — hesitation is the expensive
part. Clearing requires a named human and is audit-logged.

Auto-engages on: daily loss limit breach, reconciliation mismatch, data feed
loss, broker unreachable, repeated unhandled exceptions.

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
