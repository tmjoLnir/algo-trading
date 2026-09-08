# 27. A permission measures the settled book, a ceiling measures the committed one

**Status:** Accepted · 2026-09-07

Refines ADR 0020, which is still correct about the question it answered.

## Context

ADR 0020 found that `RiskEngine.validate` measured the wrong book. Orders are
approved one at a time and `Portfolio` moves only on a fill, so a batch of forty
entries at 5% of equity each passed a 100% cap and landed at 200%. The fix was
to project every in-flight order onto the book once, in the engine, and hand the
chain the result — so that a limit measures what the account *would* hold rather
than what it happens to hold mid-batch. That reasoning holds and nothing here
weakens it.

**But the chain does not ask one kind of question. It asks two.**

A **ceiling** asks how large this order leaves the book: `max_position_size`,
`max_gross_exposure`, `max_open_positions`, and the cash half of
`buying_power`. Those are the four ADR 0020 was written about, and the projected
book is the right one for every one of them.

A **permission** asks whether this order is allowed at all, and all three that
ask it answer with the same predicate — `reduces_position`, "is this an exit":

| Rule | The carve-out | Why it exists |
|---|---|---|
| `kill_switch` | a halt permits what reduces | a halt that refused exits froze the platform's ability to *reduce* risk (docs/SAFETY.md; day 1's F3) |
| `daily_loss_limit` | exits are never blocked | refusing a losing position's exit turns a bad day into an unbounded one |
| `buying_power` | a reduction returns cash | refusing an exit for want of buying power is perverse |

Asked of the projected book, `reduces_position` answers with a position that
does not exist. A working `BUY 100` against a flat account makes `SELL 100` look
like an exit of 100 — and all three carve-outs fire on it:

```
platform halted · settled book flat · one working BUY 100

  SELL 100  ->  kill_switch: APPROVED
```

That order opens a short while trading is stopped. Reproduced by execution, not
inferred. `docs/SAFETY.md` states the guarantee without qualification, and
`docs/ROADMAP.md`'s Phase 3 tick rests on the sentence *"engage it and confirm
orders are actually refused"*.

The other two reproduce the same way: the daily loss limit approves a new entry
past its own breach, and buying power exempts a purchase the account cannot pay
for from the one rule that exists to say so.

**And the two ceilings had the mirror-image defect.** Measuring the committed
book, they refused orders that made that book *smaller*: held 40 with 200 more
in flight, a genuine `SELL 40` leaves 200, which is over the cap, so the cap
refused the exit and left the position on. That is the same failure the kill
switch's carve-out was added to prevent, one rule along.

## Decision

**A rule receives both books and states which one it is measuring.**

```python
@dataclass(frozen=True, slots=True)
class RiskBooks:
    committed: Portfolio  # settled + everything in flight — what a ceiling measures
    settled: Portfolio  # moved only by fills — what a permission measures


def check(self, order: Order, books: RiskBooks, limits: RiskLimits) -> RiskDecision: ...
```

The engine still projects exactly once and no rule sees `pending`. What changed
is that the projection is handed to the chain *alongside* the book it was
projected from rather than instead of it.

Three properties, each chosen against the alternative:

- **Both books, not a fourth argument.** ADR 0020 rejected passing `pending` to
  every rule because nine rules would each re-derive what in-flight means for
  their limit. That objection is not answered by hiding the projection; it is
  answered by doing the projection once and letting the rule pick a *result*.
  `reduces_position` is still written once. What each rule chooses is which book
  to ask it about, which is a property of the question, not of the projection.
- **The signature changes for every rule, including the six that do not care.**
  `mypy --strict` then names every implementation and every test double that has
  not been considered. The alternative — an opt-in protocol that only the three
  permission rules implement, in the shape of `SessionAnchored` — leaves a rule
  added later silently reading the committed book. A guard nobody is told they
  have opted out of is the failure mode this codebase keeps finding.
- **A ceiling exempts an order that closes into the position without reversing
  through it**, which is `closes_without_reversing` — the same test the kill
  switch applies to a halt, drawn once rather than in three places. Not
  `reduces_position`, which is quantity-blind by design and would let a
  `SELL 300` against a long of 100 skip `max_position_size` entirely.

  > **Corrected 2026-09-07, before the paper week.** This first shipped as
  > `increases_exposure`, a pure magnitude comparison — "does this leave *more*
  > of the symbol behind" — and that has a hole in the middle. `SELL 400`
  > against a long of 200 leaves a short of 200: no larger, so exempt, while
  > being a brand-new opposite-side position opened at full size with the symbol
  > over its cap. A reversal is new risk however neatly it balances the old, and
  > the magnitude framing could not see that.
  >
  > **And it was asked of the wrong book**, which is this ADR's own defect one
  > rule along. The exemption is an *exit* question, so it reads `settled` like
  > every other exit question here. Read off `committed`, a flat account with a
  > working `BUY 100` makes `SELL 100` look like closing a long that does not
  > exist, and the cap stands aside for an order that opens a short.
  >
  > Both were found by an adversarial review of the diff that introduced them,
  > before the branch was two hours old; three independent reviewers reached the
  > second one. Recorded rather than quietly restated, because the shape of the
  > mistake — a permission question asked of the projection — is exactly what
  > this ADR is about, and it was made *in* the ADR.

The exemption is asked **before** the book is valued, so a reduction needs no
price. Refusing to shrink a position because some *other* holding is unmarked is
how layers 5 and 6 of `docs/SAFETY.md` fail together.

## Consequences

**Three rules can now refuse an order that only reduces a position**, down from
six, and they are the three that judge the order rather than the book: trading
hours, the rate limit and stale data. That set is
`rules.EXIT_BLIND_RULES`, declared once and derived from the real chain by
`test_risk_engine.py` — five documents and three docstrings quote the number and
it had been wrong in all of them at least once.

> **ADR 0029 makes it four, conditionally.** The kill switch refuses a reduction
> in a symbol the standing halt says it cannot prove. That is not a regression of
> the carve-out this ADR defends — it is the carve-out's own premise failing, so
> the exemption is void exactly there and nowhere else. `EXIT_BLIND_RULES` still
> names the three that refuse a reduction on the *order alone*, which is the
> thing a caller can be sure of without looking at anything.

`StaleDataRule` staying in that list is what lets `KillSwitchRule` remain blind
to `HaltReason`: a data-feed halt still cannot dump the book into a market
nobody can see, because the rule whose job that is refuses first.

**`RiskBooks.of(portfolio)` is the no-pending constructor**, and it is the
honest default for a caller with nothing in flight rather than a way to skip the
distinction. Every test that had passed one portfolio now passes one portfolio.

**The residual ADR 0020 named is unchanged.** A limit is still not an invariant
of the settled book: the chain prices an in-flight order at the last mark and
the fill then crosses the spread.

**What this does not fix.** `reduces_position` is still consulted for the
*projection* itself (`project_pending` drops reducing orders so a resting stop
cannot lower projected exposure), and that call is correctly about the settled
book because that is the book being projected. It is the one place the predicate
reads the same book it always did.

**Closed by ADR 0028**, and not the way this section expected. The *predicate* there is a known hole, and it is left open deliberately.
`reduces_position` is quantity-blind, so a working reversal — `SELL 300` against
a settled long of 100 — is dropped from the projection entirely, and the
committed book still shows a long of 100 when that fill would leave it short
200. A batch of flip orders is therefore invisible to both ceilings.
`closes_without_reversing` is the obvious substitute and it is *not* obviously
right: projecting that order also credits its cash, which makes `buying_power`
more permissive, and ADR 0020 chose the "reductions are not credited" asymmetry
on purpose. Changing it is a decision about ADR 0020, not a correction to this
one, and it wants its own record.

## Alternatives considered

**Project only for the ceilings and give permissions the raw portfolio, without
naming either.** This is the same behaviour with the distinction left implicit —
the engine would call some rules with one book and some with another based on a
list it maintains. Rejected: the list is the drift. A rule's docstring is where
"which book am I judging" belongs, next to the reason.

**Keep one book and fix `reduces_position` to take both.** Tempting, because the
three carve-outs all go through it. Rejected: it does not reach the two ceilings,
whose defect is not about `reduces_position` at all, and it leaves the same
single-book signature for the next rule that needs the distinction for a third
reason.

**An opt-in `SettledBookAware` protocol**, mirroring `SessionAnchored`. Rejected
above: `anchor` is an optional *capability* whose absence means "this rule has no
session boundary", and a rule that silently lacks it loses nothing. A rule that
silently reads the wrong book approves orders during a halt.

**Leave the ceilings alone and accept that they refuse exits.** Rejected on the
evidence of the rule directly above them in the chain: the kill switch was given
its carve-out precisely because refusing a protective child of a filled entry
left a position naked, and `ProtectionResult` exists to report that case. A cap
that refuses a stop reproduces it.
