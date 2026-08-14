## What and why

<!-- What changes, and what problem it solves. Link the issue. -->

## Type

- [ ] Bug fix
- [ ] Feature
- [ ] Refactor
- [ ] Docs
- [ ] Strategy / rule change

## Trading-specific checklist

<!-- Delete any section that genuinely does not apply. -->

**If this touches order flow, risk, or P&L:**
- [ ] Failure paths tested, not just the happy path
- [ ] Money handled as `Decimal`, never `float`
- [ ] All timestamps tz-aware UTC
- [ ] Orders still go through `OrderRouter` → `RiskEngine` (no new submit path)
- [ ] No risk check weakened or bypassed

**If this touches the backtest engine:**
- [ ] No lookahead introduced; fills still on the next bar
- [ ] Verified against a hand-computed fixture

**If this touches a strategy:**
- [ ] Backtested with realistic costs
- [ ] Trial count disclosed (how many variants were tried?)

## Verification

<!-- What you actually ran. Paste output for anything numerical. -->

- [ ] `make check` passes
- [ ] `docs/ROADMAP.md` ticked in this diff if this completes a roadmap item
      (or the item claimed as wip / corrected — see the top of that file)

## Risk

<!-- What could this break in production, and how would you notice? -->
