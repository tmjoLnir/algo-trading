# Risk implementation notes

Findings from auditing `docs/RISK.md` against the code that is supposed to enforce it,
recorded before Phase 3 starts rather than discovered during it.

`RISK.md` is written descriptively — "enforced by `RiskEngine` on every order", "ATR-based
stops are the default" — but it is a *specification*. When this was written almost none of
it was implemented, and in a handful of places the skeleton actively disagreed with it.
Those disagreements were the point of the file: each one is a decision someone would
otherwise have made by accident at implementation time.

**Now largely spent.** Every row of the table below is implemented, and the contradictions
are annotated in place with how each was resolved. What is left is items 5 and 8 and part of
*Smaller drift*. The one thing this file was most right about — **nothing is wired** — is
half-answered: the risk chain now has a production caller in `OrderRouter`, and the runner
that would drive it does not exist yet. Delete the file once the rest are closed or promoted
into `RISK.md` proper.

---

## Where things stand

| `RISK.md` section | Enforcement |
|---|---|
| Position accounting | **Implemented and tested.** `domain/position.py:76`, 18 tests, no skips |
| Position sizing | **Implemented and tested.** All five methods, `risk/rules.py:position_size` |
| Stop losses | **Implemented and tested.** All six types, `risk/stops.py`, 41 tests |
| Portfolio limits | **Implemented and tested.** All nine rules, `risk/rules.py`, 35 tests |
| The kill switch | **Implemented and tested.** `risk/killswitch.py`, unit + real-Redis integration |

`RiskEngine.validate` and `validate_or_raise` landed in #28; `default_rules()` landed
alongside the rules. `OrderRouter` is implemented, so the chain finally has a caller outside
tests.

**Resolved since this file was written:** items 1, 2, 3, 4, 6 and 7 below, plus the
`StaleDataRule`, `RiskDecision.shrink` and auto-engage entries under *Smaller drift*. Each is
annotated in place rather than deleted, so the reasoning survives. What remains open is items
5 and 8, and part of *Smaller drift*.

Item 5 is deliberately left open. Giving `StopSpec.type` and `PositionSizeSpec.type` default
values would mean a rule set that omits them silently gets one — and "silently gets a stop
policy" is not obviously better than "is told to state one". That is a judgement about how
much the config layer should assume, not a defect to be cleared, and it wants an opinion
from whoever owns the rule-builder UI.

Two things worth separating out, because they are different problems:

~~**Nothing is wired.**~~ **Half-answered.** `OrderRouter` is implemented, and it is the
production call site the chain never had: `RiskEngine.validate()` gates every path through
it — entries, exits, protective stops, flattens — with no way to reach a broker adapter
around it. `StopManager` and `RedisKillSwitch` now have callers there too. So "every order
passes `RiskEngine.validate()`" is true of the chain *and* of every order the platform can
currently construct.

What it is not yet is *exercised*: nothing calls `OrderRouter` in production either, because
`StrategyRunner` and the trade-updates stream are unstarted Phase 4 items. The claim has
moved one link down the chain rather than being discharged, and the roadmap says so on each
item rather than letting a tick imply otherwise.

~~**Nothing is asserted.**~~ Was ten tests and ten `pytest.skip("TODO")`. The names were
good and encoded the right cases, so they were kept and filled in; that file is now 60 tests
with no skips, and `test_stops.py` and `test_kill_switch.py` sit alongside it.

### The accounting code holds up

Worth stating positively, since it is the part everything downstream computes from.
`Position.apply_fill` implements all three documented cases correctly: it re-averages on an
add (`position.py:110-115`), leaves the basis untouched on a reduce — `avg_entry_price` is
only ever reassigned on a flip (`:129`) or on going flat (`:143`) — and on a flip realises
against `closed_qty = min(|signed_qty|, |old_qty|)` before setting the new basis to the fill
price rather than a blend (`:122-130`). `Decimal` throughout. `test_position.py` covers all
three cases plus both sides, partial-fill sequences and fees, with two Hypothesis property
tests. Clearing protective levels on flat (`:139-147`) is not in `RISK.md` and should be —
a stop left armed across a flat would reference a basis that no longer exists.

---

## Contradictions to resolve

Ordered by what they cost if missed.

### 1. `Position.exposure` reports zero for an unmarked position

`market_value` returns `Decimal(0)` when `last_price is None` (`position.py:57-59`), so
`exposure` is zero and `Portfolio.gross_exposure` (`:172-175`) silently under-reports.

This directly contradicts the engine's stated posture (`risk/engine.py:6-8`):

> rules are *deny*-oriented and default-closed. If a rule cannot evaluate — a missing mark,
> an unreachable account — it rejects rather than allows.

As written, an unpriced position makes `MaxExposureRule` compute a *smaller* number and
therefore **approve** where it should refuse — the exact inversion the design note warns
against. `Portfolio.equity` is understated the same way, which propagates to every
percentage limit.

Fix before any exposure rule is written: either have exposure raise or return `None` for an
unmarked position and make the rules treat that as a denial, or give `Portfolio` an explicit
"are all positions marked?" check that the engine consults first. Do not leave a zero that
reads as "no exposure".

**RESOLVED** — the second option. `Portfolio.unmarked_symbols` lists open positions carrying
no mark, and `MaxPositionSizeRule`, `MaxExposureRule` and `DailyLossLimitRule` deny while it
is non-empty, naming the symbols in the reason. `market_value` is unchanged, so nothing that
reads it had its semantics moved underneath it. `test_rule_that_cannot_evaluate_denies`
asserts the inversion specifically: an unmarked holding must not buy an approval.

### 2. `Order.reduces_position` does not exist

`DailyLossLimitRule` (`risk/rules.py:64-75`) tells its implementer:

> Check `order.reduces_position` before denying.

There is no such property on `Order` (`domain/order.py:36-120`). This is the doc's most
safety-critical rule — "blocks entries, never exits", because refusing to let a losing
position close turns a bad day into an unbounded one — and the API it is specified against
is not there.

It also cannot be a property of `Order` alone: whether an order reduces depends on the
current position in that symbol. A sell is an exit if you are long and an entry if you are
flat or short. The signature needs the portfolio — e.g. a module-level
`reduces_position(order, portfolio) -> bool`, or a `Portfolio` method. Decide this before
writing the rule, or the rule will be written against an ambiguity.

**RESOLVED** — module-level `reduces_position(order, portfolio)` in `risk/rules.py`, used by
`DailyLossLimitRule` and `BuyingPowerRule`. An order that flips through zero counts as
reducing: it closes the position on the way past, and refusing it would trap the very
holding the limit is trying to release. The `Order` docstring reference has been corrected
to point at the function.

### 3. The daily loss limit has nothing to measure against

`Portfolio` (`position.py:152-198`) exposes `starting_equity` — account inception, not
today — and an unbounded `equity_curve` list. There is no day-start equity, no session
boundary, and nothing that resets. `max_daily_loss_pct` is therefore uncomputable as things
stand.

Needs a deliberate answer to: what anchors "the day"? Equity at the first bar of the session,
persisted so it survives a worker restart mid-session (a restart that re-anchors to a
mid-drawdown equity silently doubles the day's allowed loss). Related: `equity_curve` grows
without bound in a long-running process.

**PARTIALLY RESOLVED.** `DailyLossLimitRule.day_start_equity` holds the anchor and
`.anchor(equity)` sets it; an unanchored rule *denies entries* rather than guessing, so the
chain refuses to trade until someone has answered the question. **Who calls `anchor()`, and
where the value is persisted across a restart, is still open** — it belongs with
`StrategyRunner` in Phase 4, which is also what owns the session boundary. Note the
consequence: assembling `default_rules()` and never anchoring gives a chain that blocks every
entry and allows every exit. That is the safe failure, not a working configuration.

`equity_curve` growing without bound is untouched and still open.

### 4. `client_order_id` is random, not deterministic

`order.py:56` generates it with `uuid.uuid4()`. Both CLAUDE.md §1.4 and the `Order` docstring
(`order.py:40-43`) promise the opposite:

> `client_order_id` is generated by us before submission and reused on every retry (rule
> §1.4). It is the idempotency key: if a submit times out, we query by this id rather than
> resubmitting blind and risking a double position.

The id is stable only while the *same object* is retried. Rebuild the order after a timeout —
from a signal, from a persisted request, after a process restart — and it mints a fresh id,
which is precisely the duplicate-position scenario the rule exists to prevent.

Derive it from something reproducible instead: strategy id, symbol, side, and the bar
timestamp that triggered it. Then the same intent produces the same key no matter how many
times it is reconstructed.

**RESOLVED** — `execution/idempotency.py`, wired into the one submission path. The key is
`sha256(strategy · symbol · side · purpose · decided_at)`, and `OrderRequest` carries
`decided_at` and `purpose` so a request persisted to a table and replayed after a restart
derives the key it derived the first time. The random default on `Order` stays, relabelled
in its docstring as the fallback for an order that never reaches a venue.

Two things the derivation had to get right that this note did not anticipate, both of them
collisions rather than duplications — the failure that reads as fine in a log, because the
venue returns the *existing* order and one leg silently never trades:

- **`purpose` is load-bearing.** Strategy id, symbol, side and bar timestamp are not
  enough. A strategy reversing on one bar — exit the long, open the short — emits two
  SELLs agreeing on all four. Without a discriminator they are one key and the strategy
  ends up flat believing it is short.
- **Quantity is out of the entry key and in the child key.** A rule that shrinks an order
  mutates `order.qty` (`engine.py:112`), so a qty-bearing key would let the shrunk order
  through as a second order — turning the one control that can *reduce* an order into the
  one that can duplicate it. A protective child is the reverse: it exists to cover a
  specific tranche, so it is keyed on the range `(covered_from, covered_to]` of the entry's
  fill it protects. Keyed on the increment instead, a 200-share entry filling 100 + 100 —
  the ordinary case — gives both stops the same key, and the second tranche is naked while
  the router books it as protected.

### 5. "ATR is the default" and "`risk_pct` is the default" are prose only

Neither `StopSpec.type` (`strategy/rules.py:107`) nor `PositionSizeSpec.type` (`:122`) has a
default value — both are required fields. Every rule set must state them explicitly, so the
documented defaults exist nowhere in code.

Worse, the one stop default that *does* exist contradicts the doc:
`RiskLimits.default_stop_loss_pct = Decimal("0.02")` (`config.py:33`) is a fixed 2% stop —
the exact thing `RISK.md:54-56` singles out as "far too tight on a volatile small-cap … you
are stopped out by ordinary noise". Either make the field's name and role explicit (a
fallback only, not a recommendation) or replace it with an ATR-based default.

### 6. Risk-per-trade is unbounded

`PositionSizeSpec.value` (`strategy/rules.py:123`) is a bare `Decimal`. Nothing rejects
`{type: risk_pct, value: 0.95}`. `RISK.md:41-42` gives 0.5–2% as the range and explains that
above 2% a normal 8–10 trade losing streak is account-threatening — advice with no validator
behind it.

Notable because the same file bounds everything else it can: `offset` is `ge=0`,
`cooldown_bars` is `ge=0`, `max_concurrent_positions` is `ge=1`. A `Field(gt=0, le=...)` here
is a one-line change and belongs with sizing. Consider a hard cap that rejects and a soft
threshold that warns, since 2% is a rule of thumb rather than a law.

**RESOLVED** — bounded, but type-aware rather than by a single `Field`, because `value` means
a different thing per method: 500 is an ordinary share count and an absurd risk fraction.
`value` is now `gt=0` always; the three fractional methods reject anything above 1 (with a
message pointing out that 0.01 is 1%, not 1); and `risk_pct` additionally rejects above
`MAX_RISK_PCT = 0.10`. That backstop is deliberately an order of magnitude past the
documented 0.5–2% rather than at it — the mistake worth catching at config time is a
misplaced decimal point, not a deliberate 3%. No soft warning: a warning that goes nowhere
a human reads is not a control.

### 7. `.env.example` omits `RISK_MAX_OPEN_POSITIONS`

Four of the five documented limits are in the operator template; the 20-position cap is not.
It still applies — `RiskLimits` defaults it (`config.py:32`) — but an operator reading the
template sees no sprawl limit and cannot tune it without reading the source.

`config.py` and `RISK.md` agree exactly on all five values otherwise (0.10 / 1.00 / 0.03 /
30 / 20), which is the one place doc and code are already in sync. Keep it that way.

**RESOLVED** — `RISK_MAX_OPEN_POSITIONS=20` and `RISK_MAX_QUOTE_AGE_SECONDS=30` are both in
the template now, and `RISK_DEFAULT_STOP_LOSS_PCT` carries a comment saying it is a fallback
rather than a recommendation (item 5's smaller half).

**SUPERSEDED (ADR 0025)** — the template no longer carries any of them, because none of them
is an environment variable any more. All eight are columns on the `worker_config` row, edited
in the risk section of the dashboard's Config tab. The concern this item raised is answered
more completely than the fix it asked for: every ceiling is now on a screen with the sentence
explaining it beside the box, so there is nothing to tune "without reading the source" and
nothing an operator can fail to notice because a template omitted it. `.env.example` keeps a
block naming the eight and their old defaults, so an operator upgrading can see what to copy
across.

### 8. `flatten_at_close` is a field nobody reads

`RiskSpec.flatten_at_close` (`strategy/rules.py:132`) exists and is never referenced
anywhere else in the repo. `RISK.md:74-76` names it as one of only *two* defences against
overnight gap risk — the other being position size — in the section explaining that a stop
is not a guarantee of price.

A strategy author can set it today and get silent no-op protection, which is worse than not
offering it. Either implement it with the session calendar or mark it explicitly unsupported
until Phase 4.

---

## Smaller drift

- ~~**`StopType.FIXED_AMOUNT`** is not in `RISK.md`'s stop table~~ — **RESOLVED.** The row is
  added and the type is implemented; it was a real stop type, not a stray member. Two of
  `RISK.md`'s own gaps went in alongside it: that protective levels are cleared when a
  position goes flat (which the *accounting* section of this file said should be documented),
  and that a level below zero is refused rather than armed.
- ~~**`HaltReason.RATE_LIMIT_STORM`** is a sixth auto-engage reason beyond the five
  `RISK.md` lists~~ — **RESOLVED.** Added to the doc's auto-engage list.
- ~~**Zero of the documented auto-engage triggers are wired.**~~ — **STALE, corrected.** Two
  are: `DATA_FEED_LOST` from the stream consumer (`data/stream.py:354`, `:515`) and
  `BROKER_UNREACHABLE` from the order router, which engages when a submit fails in transport
  and cannot be resolved against the venue — the case `RUNBOOK.md`'s "Broker unreachable →
  *Auto-halts. Confirm.*" describes. The general point stands for the remaining four
  (`DAILY_LOSS_LIMIT`, `RECONCILIATION_MISMATCH`, `RATE_LIMIT_STORM`, `UNHANDLED_EXCEPTION`):
  each belongs to whichever subsystem detects it. `RECONCILIATION_MISMATCH` is wired now:
  `scheduler.reconcile_with_broker` runs the real `Reconciler` against the runner's live
  book every five minutes during market hours, and the reconciler engages. That leaves
  `DAILY_LOSS_LIMIT`, `RATE_LIMIT_STORM` and `UNHANDLED_EXCEPTION`, all of which belong to
  the runner. `flatten_all_positions()` is **gone** rather than implemented: the act is
  `POST /api/v1/risk/flatten-all`, behind a typed confirmation, a step-up password and an
  audit row, because ADR 0005's carve-out is a human calling the broker directly and ends
  "no automated path may call either method" — and a module-level function in this package
  is reachable by every automated path there is. Halting is still not flattening, which is
  why the endpoint is separate from `engage()` and reports whether the platform was halted
  when it ran.
- ~~**`StaleDataRule.max_age_seconds = 30`** is hardcoded on the dataclass~~ — **RESOLVED.**
  Moved to `RiskLimits.max_quote_age_seconds`, so it is configurable like every other limit.
- ~~**`RiskDecision.adjusted_qty`** is specified but unreachable~~ — **RESOLVED.**
  `RiskDecision.shrink(rule, reason, qty)` exists and rejects a shrink to zero, `validate`
  applies it and mutates the order so later rules measure against the reduced quantity, and
  a test asserts a second rule sees the smaller number. No default rule shrinks — refusing is
  clearer than silently trading a fraction of what a strategy asked for — but the path is now
  whole rather than half-specified.
- **`RuleSet.max_concurrent_positions`** (default 5, `strategy/rules.py:148`) is a
  per-strategy limit that `RISK.md` does not mention alongside the account-wide
  `max_open_positions` of 20. The relationship — strategy limits may be tighter, never looser
  (`config.py:21-23`) — is stated for `RiskLimits` but not enforced anywhere.

---

## Suggested order of work

1. Fix items 1, 4, 6 and 7 first. They are small, independent of the risk engine, and each is
   a live defect in code that already ships.
2. Decide items 2, 3 and 5 before writing any rule — they are API and semantics questions,
   and a rule written against the wrong answer is harder to unpick than an unwritten one.
3. Then implement the chain, un-skipping `test_risk_engine.py` as each rule lands.
4. Wire the engine into the worker in the same change that implements it. An unwired
   `RiskEngine` enforces nothing, and a green test suite will not tell you so.
