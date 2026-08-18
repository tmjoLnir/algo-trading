# SAFETY — read this before enabling live trading

This document is not boilerplate. Every rule here exists because the failure it
prevents has happened to someone, usually to a firm with more process than you
have. Read it fully once, then treat the checklist as mandatory.

---

## The one-sentence version

**A software bug in this system does not produce a stack trace. It produces a
loss.** Most classes of bug you are used to — a wrong render, a 500, a bad
migration — are recoverable. An order loop that submits 4,000 orders in ninety
seconds is not.

## The layered defences

No single control is trusted. Each layer assumes the ones above it have failed.

| Layer | Control | Fails if |
|---|---|---|
| 1 | `ATP_RUN_MODE` defaults to `paper` | someone edits the default |
| 2 | `ATP_ALLOW_LIVE_TRADING` must also be `true` | someone sets both |
| 3 | Paper and live use different Alpaca keys | live keys deployed to the paper env |
| 4 | Every order passes `RiskEngine.validate()` | a code path bypasses `OrderRouter` |
| 5 | Broker-side stops on every position | stop never placed after the entry fill |
| 6 | Kill switch, checked before every order | Redis unreachable — **fail closed** |
| 7 | Reconciliation every 5 min, halts on mismatch | reconciliation itself is not running |
| 8 | Broker-side account limits | — |

Layer 8 matters and is outside this codebase: **set position and loss limits in
your broker's own controls too.** They are the only limits that still apply when
this platform is the thing that is broken.

## Before you go live — checklist

Do not skip items because the strategy is "simple". The simple ones are the ones
people deploy without checking.

**The strategy**
- [ ] Backtested over ≥ 2 years including a drawdown period (2018 Q4, 2020 Feb–Mar, 2022)
- [ ] Backtested with realistic costs — never `ZeroCostModel`
- [ ] Walk-forward tested, not just one in-sample fit
- [ ] Paper traded ≥ 4 weeks with results comparable to backtest
- [ ] You can state its edge in one sentence without using the word "optimised"
- [ ] Parameters were not selected by picking the best of a large sweep (see BACKTESTING.md)

**The system**
- [ ] `make check` green
- [ ] Reconciliation runs clean against the paper account — the procedure for
      a first run, and what it can and cannot prove, is `FIRST_PAPER_RUN.md`
- [ ] Kill switch tested end to end — engage it and confirm orders are actually refused
- [ ] Every strategy has a stop loss configured; there are no unprotected positions
- [ ] Data-feed disconnect tested (kill the network, confirm it halts rather than trading on stale prices)
- [ ] Worker restart tested with open positions — it must adopt them, not double them
- [ ] Alerts reach a human on a phone, not just a log file

**The account**
- [ ] Broker-side max position size and daily loss limits set
- [ ] Starting capital is money you can afford to lose entirely
- [ ] Pattern-day-trader implications understood (US accounts under $25k)
- [ ] You know how to liquidate manually through the broker's own UI, without this platform

**The human**
- [ ] Someone is reachable during market hours
- [ ] `docs/RUNBOOK.md` read; you know what to do at 09:45 when something looks wrong
- [ ] You have decided, in advance and in writing, what loss level makes you turn it off

## Rules that do not bend

1. **Never disable a risk check to make something work.** If a limit blocks a
   legitimate order, the limit is wrong and gets changed deliberately, in a
   commit, with a reason. Not with a temporary bypass that becomes permanent.
2. **Never widen a stop on a losing position.** Moving a stop away from price is
   how a planned 2% loss becomes an unplanned 40% one. Tightening is fine.
3. **Never trade a strategy you cannot explain.** If you cannot say why it makes
   money, you cannot recognise when it stops working — and it will stop working.
4. **Never deploy on a Friday afternoon**, or in the last 30 minutes of a
   session. Deploy pre-market with time to watch it.
5. **Never run an untested strategy in live "just to see".** That is what paper
   mode is for, and paper mode uses the same live data.
6. **Two independent locks stay two.** Do not "simplify" the double flag.

## Access control

**The API in this skeleton has no authentication.** `get_current_user()` is a
stub. Every endpoint under `/risk`, `/orders` and `/positions` can move money.

Until auth is implemented: bind to localhost only, never expose port 8000
publicly, and do not deploy to a cloud host with a public IP. This is a blocking
item before any deployment, and it is item one in `docs/ROADMAP.md` Phase 6.

**This is enforced now, not merely stated.** Every port in `docker-compose.yml`
binds `127.0.0.1` — the API, Postgres (whose credentials are `atp`/`atp`), the
Redis holding the kill-switch state, and the dev server alike — and
`make check-bindings` fails on any service published to `0.0.0.0`. CI runs it
before the stack starts, so a compose file that opens one of these to the
network fails the build instead of being found by a port scan. It was stated
here and contradicted by the compose file for some time, which is the argument
for the check.

The single deliberate exception is the dashboard's own port, through
`ATP_WEB_BIND_ADDR`: one LAN or VPN address, defaulting to loopback, wildcards
refused. It is safe to move only because nginx reaches the API across the
compose network, so exposing the dashboard does not expose the API with it.
docs/DASHBOARD.md has the trade-offs.

A firewall is not a substitute. Docker publishes ports with rules traversed
before the chain ufw and firewalld write into, so `ufw deny 8080` reports itself
applied and blocks nothing; restricting a published port means the `DOCKER-USER`
chain. Bind addresses are the control that holds.

## Secrets

- Keys live in `.env` (gitignored) or a secrets manager. Never in code, never in
  a commit, never in a log line, never in a screenshot in an issue.
- Paper and live keys are different pairs — never share one.
- If a key is exposed, revoke it at the broker first, then clean up the repo.
  Rotation order matters: a revoked key is harmless in a git history.

## Incident response

Something is wrong and you are not sure what:

1. **HALT.** The kill switch is on the dashboard. Do it first, diagnose second.
   A halt costs you missed opportunity; hesitation costs money.
2. Decide separately whether to **flatten**. Halting stops new risk; flattening
   realises existing P&L. A data outage means stop trading — not dump the book
   into a market you currently cannot see.
3. Check `docs/RUNBOOK.md` for the specific scenario.
4. Write down what happened before you forget it. The post-mortem is how the
   guardrail that would have caught it gets added.
