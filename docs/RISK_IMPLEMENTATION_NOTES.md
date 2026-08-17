# Risk implementation notes

Findings from auditing `docs/RISK.md` against the code that is supposed to enforce it,
recorded before Phase 3 starts rather than discovered during it.

`RISK.md` is written descriptively — "enforced by `RiskEngine` on every order", "ATR-based
stops are the default" — but it is a *specification*. Almost none of it is implemented yet,
and in a handful of places the skeleton actively disagrees with it. Those disagreements are
the point of this file: each one is a decision someone will otherwise make by accident at
implementation time.

**This file is temporary.** Delete it when Phase 3 lands and every item below is either
fixed or promoted into `RISK.md` proper.

---

## Where things stand

| `RISK.md` section | Enforcement |
|---|---|
| Position accounting | **Implemented and tested.** `domain/position.py:76`, 18 tests, no skips |
| Position sizing | **Implemented and tested.** All five methods, `risk/rules.py:position_size` |
| Stop losses | **Implemented and tested.** All six types, `risk/stops.py`, 41 tests |
| Portfolio limits | **Implemented and tested.** All nine rules, `risk/rules.py`, 35 tests |
| The kill switch | Protocol implemented against; `RedisKillSwitch` still a stub |

`RiskEngine.validate` and `validate_or_raise` landed in #28; `default_rules()` landed
alongside the rules. Every `OrderRouter` method (`execution/router.py:57-91`) still raises
`NotImplementedError`.

**Resolved since this file was written:** items 1, 2, 3, 6 and 7 below, plus the
`StaleDataRule` and `RiskDecision.shrink` entries under *Smaller drift*. Each is annotated
in place rather than deleted, so the reasoning survives. What remains open is items 4, 5 and
8, and most of *Smaller drift*.

Item 5 is deliberately left open. Giving `StopSpec.type` and `PositionSizeSpec.type` default
values would mean a rule set that omits them silently gets one — and "silently gets a stop
policy" is not obviously better than "is told to state one". That is a judgement about how
much the config layer should assume, not a defect to be cleared, and it wants an opinion
from whoever owns the rule-builder UI.

Two things worth separating out, because they are different problems:

**Nothing is wired.** Searching `libs`, `apps`, `scripts` and `tests` for `RiskEngine(`,
`OrderRouter(`, `StopManager(`, `RedisKillSwitch(`, `.validate(`, `.engage(` and
`position_size(` returns no call sites at all. Filling in the bodies is necessary but not
sufficient — there is currently no path that would reach them, so "every order passes
`RiskEngine.validate()`" has nowhere to be true. Whoever implements the engine owns
constructing it in the worker as part of the same change.

**Nothing is asserted.** `tests/unit/test_risk_engine.py` is ten tests, ten
`pytest.skip("TODO")`. The test names are good and encode the right cases — keep them, they
are a to-do list.

The roadmap is accurate about all of this: Phase 3 is entirely unticked, and the one Phase 0
item that *is* ticked (`Position.apply_fill`) genuinely holds. No roadmap correction needed.

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
- **`HaltReason.RATE_LIMIT_STORM`** (`risk/killswitch.py:35`) is a sixth auto-engage reason
  beyond the five `RISK.md` lists. The other five all map cleanly. Add it to the doc.
- **Zero of the documented auto-engage triggers are wired.** No caller of `engage()` exists.
  Each of the five is a separate piece of work in whichever subsystem detects it —
  reconciliation, the stream consumer, the broker adapter — not something the kill switch
  module can do alone.
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
