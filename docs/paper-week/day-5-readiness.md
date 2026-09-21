# Day 5 Readiness Check

**Repository:** `tmjoLnir/algo-trading` · **Commit audited:** `8e3be3c` · **Date:** 2026-09-21
**Scope:** every item in `docs/paper-week/day-4-review.md` §8 — B1, B2 and F1–F13 — against the
code that claims to fix them, across PRs #157–#162.

**Method.** Each item was read from the day-4 review, traced in the tree at HEAD by an independent
verifier, then attacked by two further agents — one asked whether the fixed code is ever *reached*,
one asked what happens on the *failure* path. Three sweeps ran alongside: would day 5 actually
trade, a regression hunt over the ~700 lines that landed, and an operator-readiness pass. A
completeness critic then attacked the whole result. The blocking conclusion in §3.1 was reproduced
by execution, with a control, rather than by reading. Build gates were run.

---

## 1. The verdict

**Of §8's nine items, one landed, one landed with the half that day 5 needs missing, one is
analysis that was never applied, and six were not started.**

The code that did land is good. `OrderRouter._close` is a real fix with a real call site, eight
tests, and a `FakeBroker` that now models the venue reservation which let day 4 ship. #159's
timeframe analysis is the most valuable document produced in the paper week so far. Neither PR
overclaims — both name their own residuals in docstrings and in `docs/RISK.md`. That should be
said plainly before the rest of this.

**But B1's fix releases only the stops the running process placed, and day 5 opens holding
positions whose stops were placed by a process that is gone.** `_release_protection` reads
`OrderRouter._protective` (`router.py:1042`), an in-memory dict that `router.py:294-298`
documents as not surviving a restart and that nothing rebuilds at boot. On an inherited
position the venue refuses the close, the release finds nothing, and `_close` returns the
refusal having cancelled nothing. Day 4 repeats exactly, on exactly the positions §8 called
urgent.

I reproduced this at HEAD, with a control:

```
router A  : position open, venue-side stop working (open_stops=1)
router B  : _protective at boot = {}

--- day 5's first exit signal on an inherited position ---
  submitted        : False
  inventory_held   : True
  risk decision    : approved=True rule='' reason=''
  reject_reason    : {"available":"0","code":40310000,"existing_qty":"100",
                      "held_for_orders":"100","message":"insufficient qty available ..."}
  cancels sent     : []
  stop still live  : 1
  position qty     : 100

--- control: the SAME close through router A, which placed the stop ---
  submitted        : True
  cancels sent     : ['brk-1']
```

**And the premise §8 was written against has expired.** Day 4 was Friday 2026-09-11. Today is
Monday 2026-09-21. Five full sessions — 09-14 through 09-18 — have passed with those GTC stops
live and unattended. The nine positions are very probably gone. **The platform cannot tell you
either way**, because B2 did not land: the only stored book is the snapshot the evaluate loop
happened to write at day 4's evaluation 353, and `restored_book` still carries no age.

**Recommendation: do not run day 5 on this commit as a measured session.** Two things must
happen before the open, and neither is a code change. §7 has the sequence.

### Build gates

| Gate | Result |
|---|---|
| `ruff check` + `ruff format --check` | **pass** — 346 files |
| `mypy libs apps tests` | **pass** — 241 source files |
| `pytest tests/unit` | **3,047 passed**, 0 failed, 0 skipped, 162 s |
| `npm run lint` / `tsc --noEmit` | **not run** — `apps/web/node_modules` absent in this container |

No flakes across three full runs, including the known `test_alpaca_provider` rate-limit flake.
The web half is *not installed*, which is a different sentence from *broken*; nothing is known
about web lint or formatting from this check.

834 new test lines landed across seven files since `c48faa1`, and they are real failure-path
tests. `test_the_fake_actually_reserves_the_shares` asserts the `FakeBroker` guard itself, so
the other seven in that class cannot silently stop covering anything.

---

## 2. Status of every §8 item

| | Item | Asked for | Actual |
|---|---|---|---|
| **1** | Fix the exit path (B1) | before day 5 | **partial** — works for this process's own stops; inert for inherited ones (§3.1) |
| **2** | Decide about the overnight positions | before day 5 | **not done** — and the premise expired (§2a); no decision recorded anywhere in `docs/` |
| **3** | Persist the book outside the evaluate loop (B2) | before day 5 | **not started** — zero lines changed (§4.1) |
| **4** | Unprotected alert in both directions (F1, F2) | before day 5 | **not started** — and day 5 makes it worse (§3.4) |
| **5** | Gate evaluation on warmup (F6) | before day 5 | **fixed** — with a new exit-blocking side effect (§3.3) |
| **6** | Stop protecting against a working parent (F5) | before day 5 | **not started** (§4.3) |
| **7** | Fix the daily report's four numbers (F4) | before day 5 | **not started** (§4.2) |
| **8** | RTH coverage in the report (F11) | before day 5 | **not started** (§4.2) |
| **9** | Then tune the parameters (F7, F8) | after item 1 | **measured, not applied** — the config row still says `1m` (§3.2) |

And the findings §8 did not enumerate:

| | Finding | Actual |
|---|---|---|
| F3 | Venue rejection reported as a risk refusal, fields blank | **not fixed** — reproduced in the probe above: `approved=True rule='' reason=''` |
| F9 | Boot-race refuses a recovered position's stop, never re-arms | **not fixed** — nothing in `risk/` or the recovery path changed |
| F10 | Worker logs nothing on shutdown | **not fixed** — which is why B2's shutdown snapshot has no foundation |
| F12/F13 | Papercuts — fee double-settle, nginx `Date`, WS reconnects, bar-write amplification | **not fixed** |

One verifiable summary of the six that did not start:

```
git diff --stat 849c461 HEAD -- libs/core/src/atp_core/alerts \
    libs/core/src/atp_core/risk libs/core/src/atp_core/dashboard apps/api
```

returns empty.

---

## 2a. The premise has expired, and the platform cannot say so

§8's urgency rests on one sentence: *"Day 5 opens holding $44,277 of inventory that can only
leave at its stop."* That was written on 2026-09-11 about nine positions — KO 56, XOM 30,
AMZN 19, INTC 48, WMT 46, MSFT 10, PEP 36, CSCO 44, IWM 17.

`TradingCalendar` at HEAD, run rather than reasoned about:

```
next_open after 2026-09-11 20:00Z → 2026-09-14 13:30:00+00:00
2026-09-21 Mon 13:30:00Z → 20:00:00Z   (today; an ordinary full session)
```

Five sessions have come and gone. Those stops were GTC and keep working whether or not the
worker runs, and #159 measured them at a median **0.121% of price** on a 1m ATR×2. Nine
positions with twelve-basis-point stops, unattended for five sessions, are almost certainly all
stopped out.

**Nobody in this repository can confirm that.** B2 is unfixed, so the only stored book is the
one `_persist` wrote at day 4's evaluation 353 (19:59:05Z) — which already excluded DIA, stopped
out 28 seconds later. `worker.restored_book` (`trading.py:333-339`) logs positions and cash and
no age, so a ten-day-old book is announced in the same words as a ten-second-old one. That is
B2 stated as a consequence rather than as a finding.

**Every severity in this document that rests on "the nine positions" is conditional on a
venue read that has not happened.** `uv run python scripts/status.py` is the answer and it is
the first item in §7. What is *not* conditional is the defect: the moment day 5 opens a position
and the worker is restarted for any reason — as day 4's operator did at 14:04:33 — the same
hole is live against the same money.

---

## 2b. Would day 5 actually trade?

**Yes, partially — and it would be the fifth consecutive session whose result means nothing,
for a new reason stacked on the old one.**

**The configuration is unchanged.** The worker reads its row from the database, not from code
(`main.py:182-195`). `git diff --stat 849c461 HEAD` touches neither `worker/config.py` nor
`infra/` nor `main.py`, so revision 6 boots exactly as it did: `sma_crossover`, 20 symbols,
Alpaca paper, IEX, `run_mode=paper`, **`timeframe=1m`**.

**What the platform's own backtest says about that configuration.** #159 measured 26 cells over
346,643 real bars through the same `indicators.dispatch` the live runner calls. All 17 intraday
cells lose money; all 9 daily cells make money. The shipped 20/50 ×2 at `1m` returns **−58.23%
with a 0.86% win rate**, and widening the stop from ×2 to ×8 moves it to −60.23% — *the stop was
never the binding constraint*. The same pairing at `1d` returns +23.63% over 5.75 years at
Sharpe 0.59, which the document itself calls "not obviously broken", not "good".

So a clean day 5 at `1m` measures the cost of crossing the spread 2,667 times a month. It does
not measure `sma_crossover`.

**What would work.** For a position day 5 opens *itself*, the exit path is genuinely fixed:
`submit_signal` → `_close` → `InventoryHeldError` → release → retry → re-arm, with a call site
at `runner.py:1649` and eight tests behind it. If the nine legacy positions are already gone —
likely — day 5 would exercise the strategy's exit rule for the first time in the paper week.
That is real, and it is the first time it has been true.

**What would still go wrong**, in the order it would happen:

1. At 13:30Z the boot restores a ten-day-old book and announces it as current (§2a).
2. `reconcile` may raise on five sessions of venue activity no tracked order explains, in which
   case `_quarantine` halts and parks and day 5 does not trade at all. That is the day-3 fix
   behaving correctly; it is still a live branch with only `scripts/adopt_broker_state.py` as
   the way out.
3. The first evaluation pages one false CRITICAL about protected positions, then goes silent
   for the session (§3.4).
4. For the first ~52 minutes no signal of any kind may trade, including an exit (§3.3).
5. Any inherited position is refused its exit all day, and the dashboard's close button cannot
   rescue it (§3.1, §3.2).
6. At 20:30Z the only end-of-day artifact prints four numbers that are wrong, and the one
   number the paper week is supposed to be accumulating is still not computed (§4.2).

---

## 3. Blocking — before day 5 runs

### 3.1 The release reads a map that a restart empties, so an inherited position cannot exit `blocker`

**What was asked.** §8 item 1: cancel protection before submitting a close, re-arm over any
remainder, treat `insufficient qty available` as a platform bug loudly, and make the fake
reserve inventory.

**What landed**, and it is a defensible inversion rather than a miss: `_close`
(`router.py:452-529`) submits *first*, and releases protection only once the venue has said in
so many words that one of our own orders holds the shares. The docstring argues it well —
releasing up front against a venue refusing for some *other* reason would cancel the stop,
fail the close, fail the re-arm, and leave the position naked by a path that did not previously
exist. That reasoning is right and the ordering is better than the one §8 asked for.

**The hole is one layer down.** `_release_protection` (`router.py:1029`) reads
`self._protective`. That dict is declared empty at `router.py:298` and written in exactly two
places — `submit_protective_orders` (`:733`) and `_rearm` (`:1152`) — both of which require
*this* process to have placed the stop. Nothing rebuilds it at boot:

- `warmup` restores open orders into the **runner's** `_open_orders` (`runner.py:598-606`),
  never into the router.
- `Reconciler` records a pre-restart stop as an orphan and explicitly does not adopt it
  (`reconciliation.py:456-462`, "Reported, never cancelled").
- The codebase already says so, twice, in the file the fix was written in:
  `router.py:787-789` (*"protective orders placed before a restart are not in here"*) and
  `runner.py:441-444` (*"the router's protective map is in-process and is not rebuilt at
  start"*).

So the path on day 5 is: `_close` submits → Alpaca answers `insufficient qty available` →
`inventory_held=True` → `_release_protection` hits `if not holding: return []` at
`router.py:1048` → `_close` returns the refusal at `:522` having sent zero cancels.

**Reproduced, not inferred.** The probe in §1 builds a protected long through one router, then
closes it through a fresh router against the same venue. The inherited close is refused with
`cancels sent: []` and the stop still live; the identical close through the router that placed
the stop succeeds and cancels `brk-1`. The only difference between the two runs is which process
placed the stop.

**Two aggravations.** The early return at `:1048` sits *before* the
`order.protection_released` log at `:1073`, so there is no line anywhere saying "we looked for a
stop to release and found none" — the only trace is `order.broker_rejected`, day 4's exact
silent signature. And `_close`'s final comment, *"the close was accepted"*, is false on the
branch it sits on.

**Why no test caught it.** All eight tests in `TestClosingReleasesProtection` build their stop
through `_open_protected`, which calls `submit_protective_orders` on the *same* router instance
(`test_order_router.py:1554`). `test_a_close_with_nothing_to_release_cancels_nothing` (`:1681`)
looks like the restart case and is not: it uses `book(SPY=(100,100))` without setting
`broker.positions`, so the fake's `_guard_inventory` returns early and the close succeeds for a
different reason. **There is no test anywhere for closing a position whose stop this process did
not place.**

**The fix, and it is small.** `_release_protection` should fall back to
`await self.broker.get_open_orders()` when its in-process map is empty — exactly what
`cancel_all` already does, for exactly this reason (`router.py:856`: *"a cancel-all driven from
a stale cache would leave precisely the orders it did not know about — which, after a restart,
are the ones most likely to be there"*). The test the suite is missing writes itself.

### 3.2 The timeframe row still says `1m`, and two operator pages disagree about it `blocker for measurability`

`docs/paper-week/f8-timeframe-and-stop-sizing.md` ("What to do") says: set the live
`WorkerConfig` timeframe back to `1d`, leave the stop at `atr ×2 period=14`, do not take periods
from the table. It flags explicitly that this *"is a configuration row an operator edits on the
dashboard, not something code can change"*.

Nobody has made that edit. No runbook step asks for it. `worker.config_loaded` would log
`timeframe=1m` again.

**And `docs/FIRST_PAPER_RUN.md` — the page an operator reads before the open — argues the
opposite.** At `:302` it tells them to *"set `strategy_params` to `{"timeframe": "1m"}` on the
Config tab if a minute series is what you want"*. Two pages in `docs/`, landed five days apart,
give contradictory configuration advice, and the stale one is the one named "The first paper
run".

**This is the single highest-leverage change available before the open, and it costs no code.**
It also dissolves three other problems: `_warmup_floor` returns `None` for daily and coarser
(`runner.py:536`), so warmup loads 51 real daily bars from Postgres and every symbol is warm at
the open — §3.3 never bites; the stop widens from ~12bp to ~4.08%, wide enough for a position to
survive to its exit signal; and the trade rate drops to about one decision per symbol per day,
which is what the engine was validated against.

**With one precondition that will otherwise produce a silent session.** §3.3's gate is real now.
Switching to `1d` with no daily bars in the store makes every symbol cold and discards every
signal, including every exit, all session. **Backfill before saving the row**, not after.

The honest caveat, which #159 states: one daily session is one bar per symbol. A week of `1d` is
five decisions per symbol, not two thousand. This does not make day 5 conclusive. It makes day 5
worth recording.

### 3.3 The warmup gate discards exits, not just entries `blocker at 1m`

`_poll_strategy` (`runner.py:1599`) discards every signal on a symbol with `have <= warm_after`.
The only exemption is `HOLD` (`:1604`) — **`EXIT` is not exempt.**

`warm_after` is `strategy.warmup_bars` exactly, unfloored, deliberately mirroring
`BacktestEngine.run` (`runner.py:482-500`). For `sma_crossover` that is `slow_period + 1` = 51.
`_warmup_floor` (`runner.py:530-543`) pins an intraday series to *this session's* open, and
`warmup` re-runs at every open. So on a `1m` day 5, every symbol starts at ~0 bars and
`have > 51` first becomes true around **14:22Z — roughly 52 minutes, 13% of RTH, in which no
position can be exited by signal.**

The backtest analogue is not equivalent, and this is the part worth naming: a backtest starts
flat, so `seen <= warmup` never gates a decision about inventory carried in from before the
window. Live it does, at every session open, against whatever the platform is holding. **That is
the divergence class the fix was written to close, pointing the other way.**

None of the five new tests in that class covers a discarded `EXIT` on an open position — they
are all entries.

At `1d` this evaporates: no floor, 51 stored bars, warm at the open. Which is another reason
§3.2 comes first.

### 3.4 The first evaluation will page one false CRITICAL, then go silent for the session `high`

`_mark_broker_protection` (`runner.py:1156-1191`) sets `position.broker_protected_qty` from
`router.broker_side_protected_qty` — which reads the same empty `_protective` map. **Every
inherited position therefore reports `unprotected_qty == abs(qty)`** while its GTC stop sits
live at the venue.

So the first evaluation sets `metrics.positions_unprotected(N)` and `_announce_unprotected`
pages CRITICAL: *"N position(s) with NO stop at the broker"*. That page is false. The docstring
at `runner.py:1175-1177` claims *"a stop resting from before a restart moves the router's
count"*; it does not.

Then F1 makes it worse rather than better. `_announce_unprotected` is byte-identical to day 4:
the dedup at `runner.py:1244` returns early on exact set equality, with no floor for a set
containing a new symbol and no periodic rollup. The set does not change, so **nothing repeats
and the all-clear never fires** — one false page, then silence for the rest of the session,
including for any genuinely naked position that appears.

F2 is likewise untouched: `runner.py:2125` still reads `refusals=[r.decision.reason ...]`, the
*risk* decision's reason, which the probe in §1 confirms is empty for a venue rejection
(`approved=True rule='' reason=''`). `refusals=['']` again.

**The operator consequence is specific and worth deciding in advance:** `docs/RUNBOOK.md:1021`
walks a reader receiving this alert through placing a stop by hand through the broker's UI.
Followed here, that produces a second stop over a position that already has one. Decide now not
to act on that page.

### 3.5 The daily-loss anchor is taken on day-4's marks `high`

`warmup` anchors the session at `runner.py:651` — `anchor_session(portfolio.equity)` — and
`_mark` is step 1 of `_evaluate_once` (`runner.py:1068`), which cannot run until warmup has
returned. There is no `_mark` anywhere in warmup's body; I checked the whole of `565-655`.

`Portfolio.equity` is cash plus market value computed from each position's `last_price`,
restored verbatim from the snapshot. **So day 5's daily-loss anchor is cash plus the inherited
positions priced at Friday-before-last's close.** The first `_mark` reprices them at today's
quotes, and `DailyLossLimitRule.check` measures the difference against that stale anchor
(`rules.py:641`), with `max_daily_loss_pct` defaulting to 0.03.

`StrategyRunner._escalate` (`runner.py:749-756`) turns the first such refusal into
`kill_switch.engage(HaltScope.GLOBAL, DAILY_LOSS_LIMIT)`, and `rollover_daily_counters` only
releases a halt engaged on a strictly earlier date — so a halt engaged at 13:35 stands for the
whole session.

**Day 5 is the first session that opens holding inventory across a non-trading gap, so this is
the first session in which it can fire.** Exits are carved out of the kill switch, so it is not
a money risk; it is a measurability risk and an alert-credibility risk. It disappears entirely
if the book is flat at the open, which is the strongest argument in §7 for flattening.

Related and worth recording while it is in view: `day_start_equity`'s own docstring
(`rules.py:606-608`) says it is *"persisted there so a mid-session restart does not re-anchor to
a drawn-down number and silently grant the day a second allowance."* Grep finds no column, no
Redis key, no snapshot field — six hits, all inside `rules.py`. The guarantee is asserted in one
docstring, warned about in a second (`engine.py:275`), and implemented nowhere. A restart to
clear the halt above would grant day 5 a fresh 3% on top of the loss already taken.

---

## 4. Not fixed, and carried from day 4

### 4.1 B2 — the book is still written only from inside the evaluate loop `high`

Zero lines changed. `_persist` (`runner.py:1130`) still has exactly one caller — step 6 of
`evaluate` at `runner.py:1073`. `PortfolioRepository.snapshot` has two call sites in the whole
tree: that one and `scripts/adopt_broker_state.py:151`. No snapshot on fill, none from the
reconcile job, none on shutdown — and the shutdown one had no foundation anyway, because F10's
signal handler never landed either. `restored_book` still carries no age.

The consequence is §2a: the platform cannot answer what it holds without asking the venue, and
every clean reconcile on day 5 will be worth exactly what day 4's 78 were worth, because the
book may have been reconstructed from the broker it is being checked against.

`docs/ROADMAP.md:1627-1644` states this correctly and in the same diff as the day-4 review,
which is CLAUDE.md §6 working as intended.

### 4.2 F4 and F11 — the only end-of-day artifact still prints four wrong numbers `high`

`libs/core/src/atp_core/analytics/daily.py` and `paper_run.py` do not appear in
`git diff --stat c48faa1..HEAD`. Submitted-vs-accepted, refused-vs-refused-by-risk, the
24-hour rolling window, and the missing P&L are all as day 4 left them. Nothing computes RTH
coverage — minutes with an evaluating runner against minutes of RTH — which is the number the
paper week exists to accumulate.

F3 rides with it: `runner.py:1664` still increments `orders_rejected_by_risk` and
`runner.py:1667-1674` still logs `runner.signal_refused rule= reason=` for a venue refusal,
because the rule string is empty and the branch only exempts `NO_ACTION`. The probe in §1
confirms the shape. **So a day-5 recurrence of B1 would be narrated in the logs exactly as day 4
was**, and an operator reading them would again conclude the risk configuration is too tight.

F3's own severity is observability, not blocking — the blocking weight it was assigned during
this check is borrowed from §3.1 and belongs there.

### 4.3 F5 — protection is still submitted against a working parent `medium`

`_protect` is still called on every fill (`runner.py:2001`) with no `is_complete` gate, and
`submit_protective_orders` still places per tranche. Day 4's 69 wash-trade rejections, 69 short
unprotected windows, and the alert-budget exhaustion in the first thirty seconds all recur. Not
blocking on its own; it is what makes §3.4's silence expensive, because it is what fills the
budget before anything real happens.

### 4.4 F9 — a stop refused at boot is never re-armed `medium`

Nothing in `libs/core/src/atp_core/risk/` or the recovery path changed. The ordering that
refused MSFT's stop with `stale_data` 1.2 seconds before `stream_connected` is intact, and there
is still no retry. Less likely to bite on day 5 — the inherited orders are closing stops, not
new entries — but live for anything that fills during the boot window.

### 4.5 A latent hazard that is *not* reachable on this configuration `noted`

`_cancel_stale_protection` (`router.py:988`) reads the same empty `_protective` map, and its own
docstring names the consequence: *"a stop we could not cancel is a live order that will **open**
a position rather than close one."* After a restart it returns 0 and a wrong-side inherited stop
stays resting.

It is recorded here because it is the only one of the three empty-map readers whose failure mode
is a *new unintended position* rather than a refused order, and because a fix for §3.1 must
cover this call site too or it will be fixed in one of three places.

**It is not reachable on day 5 as configured.** `sma_crossover` emits only `ENTER_LONG` and
`EXIT` — it never flips a position through zero. It becomes reachable the moment a strategy that
shorts is configured, which is a thing to remember rather than a thing to do now.

### 4.6 Documentation that now contradicts the tree or itself `medium`

- **`docs/FIRST_PAPER_RUN.md:8-9`** — *"Nothing in it has met Alpaca."* Day 4 placed 209 venue
  orders, took 236 fills and reconciled clean 78 times. `docs/ROADMAP.md:1649` repeats it two
  lines under Phase 4's *Verifiable:* line, while the same file at `:1629` narrates day 4's
  bounce in detail. A reader opening the roadmap to ask "has the paper week started?" is told no.
- **`docs/FIRST_PAPER_RUN.md:302` vs `f8-timeframe-and-stop-sizing.md`** — opposite advice on
  the one setting that decides whether day 5 means anything (§3.2).
- **`docs/RUNBOOK.md`, "A stop released for a close"** — describes `_close` as cancelling stops
  *"once the venue names one of our own orders as the holder"*, with no qualifier that "our own"
  means *this process's*. For an inherited position that paragraph is false, and it is the
  paragraph an operator would read while trying to get out of one.
- **`docs/RISK.md:123-125`** — presents the armed engine-side level as the mitigation covering
  the release gap. `_exit_reason` (`runner.py:1486`) declines to watch that level whenever
  `broker_side` is set and `_stop_is_missing` is False, and `_stop_is_missing` (`:1531`) returns
  False for any symbol absent from `self._unprotected` — which the release/re-arm path never
  writes. `_disarm_if_flat`'s docstring (`runner.py:2041-2045`) calls that exact state the worst
  one in the system. Two pages of the same tree, four days apart, disagree about whether it is
  mitigated.
- **`runner.signal_discarded_cold`** appears in `docs/` zero times. It is now the most likely
  cause of a silent session, and the symptom is indistinguishable from the strategy simply not
  crossing.

---

## 5. What landed, and it is good work

Stated separately so it is not lost in the above.

- **`_close` is correctly centralised.** The `EXIT` branch of `submit_signal` (`router.py:396`)
  and `flatten` (`:966`) both route to it, and those are the only two close paths in the tree.
  One submit path, as CLAUDE.md §1.5 requires.
- **The ordering argument is better than the one §8 asked for**, and the docstring makes the
  case rather than asserting it.
- **`tests/fakes.py` now reserves inventory by default**, and `test_the_fake_actually_reserves_
  the_shares` guards the guard. The specific reason day 4 shipped is closed.
- **CLAUDE.md §1 holds across the whole ~700-line diff.** No float touches money or quantities.
  No `datetime.now()`. `_rearm` deliberately reuses the cancelled child's `created_at` rather
  than minting a fresh one. Both `_close` submits go through `submit()` → `_route()` →
  `risk_engine.validate()`, and `_rearm`'s replacement stop is validated like any other child.
  No guard was widened and no assertion weakened.
- **Rule §1.4 holds on the retry, and this was worth settling.** The probe's control run shows
  the rejected close and the accepted retry carrying the *same* `client_order_id`
  (`atp-884513f1...`), because `_close` retries with the identical `OrderRequest`. The new
  `attempt` parameter in `idempotency.py` is appended only when non-zero, so every key minted
  before it existed is byte-identical. A retried close cannot become a second position.
- **#158's warmup gate is done at the call site**, unfloored to mirror the backtest exactly,
  counted on `signals_discarded_cold`, logged per occurrence, and surfaced on `runner.evaluated`.
  The side effect in §3.3 is a real cost, but the fix itself is the right shape.
- **#159 is the best document in the paper week.** 346,643 real bars, 26 configurations, explicit
  trial-count and overfitting disclosure, and a conclusion that contradicts the review that
  commissioned it — *the stop was never the problem.* Its only failure is that nobody acted on it.
- **The roadmap took no tick, correctly**, and carries the day-4 correction in the same diff as
  the review. `test_roadmap_summary.py` and `test_api_doc_routes.py` pass; 21 `- [x]` + 30
  `- [ ]` = 51 matches the summary table line for line.

---

## 6. Test-coverage notes

- **No test closes a position whose stop this process did not place.** This is the gap that let
  §3.1 land looking complete, and it is a four-line test: build protection through one router,
  close through another, assert a cancel was sent.
- **No test discards an `EXIT` on an open position** (§3.3). The five cold-symbol tests are all
  entries.
- **The cancel is fire-and-forget and the fake makes it synchronous.**
  `_release_protection` treats a non-exception as released (`router.py:1055-1062`);
  `AlpacaBroker.cancel_order` returns on 204, and Alpaca moves an order through `pending_cancel`
  before `canceled` — `held_for_orders` is released only at the terminal state.
  `FakeBroker.cancel_order` flips the status in the same call, so the retry always sees
  `available: 100`. Every assertion in `TestClosingReleasesProtection` rests on that. There is no
  test in which the cancel is acknowledged asynchronously, and in production a retry landing
  inside that window is refused again, fires `_rearm`, and submits a second stop the venue also
  refuses.
- **The failed-re-arm branch has no test at all.** `test_a_refused_close_puts_the_stop_back`
  covers the re-arm succeeding; nothing covers `_route(replacement)` being refused — which is
  the branch that emits `order.position_unprotected` and reaches the worst state in the system.
- **No test submits a `LIMIT` close.** `submit_signal` now pins `GTC` on any close while keeping
  `LIMIT` when the signal carries a price, and `_close` cancels protection the moment the venue
  *acknowledges*. A GTC limit that never trades, over a position whose stop is already cancelled,
  is exactly the state `_disarm_if_flat` refused to create. Latent for `sma_crossover`, whose
  exits carry no limit price; live for any `compile_ruleset` strategy or operator signal that
  sets one.
- **`tests/integration/test_kill_switch.py` carries no `pytest.mark.integration`**, so its five
  tests are deselected by CI and by `make test-integration` (AUDIT.md #13, open). Every failure
  path day 5 can take ends at the kill switch.

---

## 7. Before the open

Today is a trading day: **13:30–20:00Z**. These are ordered, and the first is not optional.

1. **Read the venue.** `uv run python scripts/status.py`. It reports halts, quote freshness, the
   latest stored bar per symbol, and the venue's account, positions and working orders. **Do not
   plan from §2a's figures or from the dashboard** — B2 means the stored book is from
   2026-09-11 and the review's numbers are ten days old. Everything below branches on what this
   prints.

2. **Decide about whatever is actually held — and know that §8's second option does not work.**
   §8 offered "flatten them by hand with the stops cancelled first, or leave them and watch the
   first exit signal on each." The second is not available: §3.1 means an inherited position
   cannot be exited by signal at all. The choice is flatten, or accept that those symbols can
   only leave at their stop for the whole session.

3. **If flattening, the ordering matters and two of the three paths are wrong.**
   - `POST /positions/{symbol}/close` **will be refused** — the API builds a fresh `OrderRouter`
     per request (`apps/api/src/atp_api/execution.py:106`, and it says so at `:67-73`), so
     `_protective` is empty on *every* API call. This is AUDIT.md finding #1, open since
     2026-08-27.
   - `POST /orders/cancel-all?symbol=X` **does** work — `cancel_all` asks the venue what is open
     rather than trusting the cache — then close. The runbook documents this only as a hazard,
     never as the enabling step it is here.
   - `POST /risk/flatten-all` gets the ordering right (`close_all_positions(cancel_orders=true)`)
     but is all-or-nothing, and needs the password plus the literal phrase.
   - Alpaca's own UI is the fourth and is what SAFETY.md layer 8 falls back to.

   Halt first in every case (`scripts/halt.py engage`), and re-run `status.py` afterwards to
   confirm both positions *and* working orders are gone.

4. **Set the timeframe to `1d` (§3.2) — and backfill before saving the row, not after.**
   `uv run python scripts/backfill_bars.py --symbols <the saved watchlist> --timeframe 1d
   --start 2021-01-01 --verify`. `sma_crossover` needs 51 bars and the gate is unforgiving. Then
   save on the dashboard's Config tab and check for `worker_config.unchanged` at WARNING, which
   means the save did nothing.

5. **`docker compose restart worker`** — the worker reads its configuration once, at start.
   Pre-market, never mid-session.

6. **`make preflight`.** It checks run mode, credentials, the three locks, strategy/timeframe
   agreement, warmup, sizing reachability, quote freshness, halts and the venue account, and
   FAILs with the fixing command. **Note what it does not check: the book.** On a day that opens
   holding inventory, the command that answers "is this ready?" does not ask the one question
   that matters — which is why step 1 is a separate step and comes first.

7. **The three locks.** For paper: `ATP_RUN_MODE=paper`, `ATP_ALLOW_LIVE_TRADING=false`,
   `worker_config.allow_live_orders=false` (inert in paper — `trading.decide` checks it only
   when `settings.is_live`). Confirm lock 1 is *present* in `.env` rather than defaulting:
   `make secrets-install` strips both file-locks wholesale (`FORBIDDEN_KEYS`,
   `manage_secrets.py:88`) and warns only the live operator, and `.env.example:39` ships
   `ATP_RUN_MODE=backtest`, which produces a worker that boots, logs, and places nothing all day.
   Drift lands on paper, so it cannot drift toward live — but that is the default, not a control.

8. **The host invariant (ADR 0033).** `pmset -g log | grep -i "Total Sleep/Wakes"` before the
   session and again after the close; it must not increase. Baseline at 2026-09-11 was 5.
   **Nothing in this repository stores, compares or alerts on that integer** — day 4's closing
   reading is not written down anywhere in the tree, so if the machine has rebooted the baseline
   is gone. Also confirm `SleepDisabled 1` and `sleep 0` still hold.

9. **`uv run python scripts/check_alerts.py --by "<you>"` — and look at the phone.** RTH is
   21:30–04:00 the operator's local time. ADR 0033 makes a delivered notification a
   precondition, not a checklist item, and the script reports delivery rather than receipt.

10. **Accept in advance** that the first evaluation may page one false CRITICAL about protected
    positions and then go quiet (§3.4). Do not hand-place a second stop in response.

11. **Watch the boot** (`docker compose logs -f worker`): `worker.config_loaded` — quote it,
    do not screenshot the form; `worker.restored_book` vs `worker.adopted_broker_state` (the
    first means the reconcile was a real check, the second means it was clean by construction);
    `runner.restored_open_orders`; `execution.reconcile.clean`; `runner.warmed_up bars=N
    short=S`; `worker.ready trading=True halted=False`.

**And decide, in writing, whether day 5 counts.** §8 argued the week should start when the first
session ends with a result that means something about the strategy. On this commit that requires
steps 2 and 4 to both have happened. If they have not, run the session as a shakedown and say so
in the review — a Phase 4 tick taken from an unmeasured session is the failure CLAUDE.md §6
exists to prevent.

---

## 8. Recommended order of work

Before the open, in the session itself — no code:

1. `scripts/status.py`, and decide about whatever it shows (§7.1–3).
2. Backfill daily bars and set the timeframe to `1d` (§7.4). **This is the highest-leverage
   change available and it costs nothing.**

Then, as code, in this order:

3. **`_release_protection` falls back to the venue when its map is empty** (§3.1) — and the same
   fallback must cover `_cancel_stale_protection` (§4.5) and `cancel_protection`, or it will be
   fixed in one place of three. Ships with the test the suite is missing.
4. **Exempt `EXIT` from the cold gate, or bound it** (§3.3). A position already held is not a
   cold-start decision; discarding its exit is strictly worse than letting it through.
5. **Mark the book before anchoring the session** (§3.5), or anchor from fresh quotes. And
   persist `day_start_equity`, which two docstrings already promise.
6. **B2: snapshot on fill and from the reconcile job, and put the snapshot's age in
   `restored_book`** (§4.1). The shutdown snapshot waits on F10's handler; the other two do not.
7. **The unprotected alert in both directions, plus the refusal text** (§3.4, F1/F2) — and fix
   `_mark_broker_protection` so an inherited stop counts, which is the same fallback as item 3.
8. **The daily report's four numbers and RTH coverage** (§4.2). It is the only end-of-day
   artifact and it finally runs.
9. **Await the venue's cancel acknowledgement rather than the request** (§6), and add the
   async-cancel test.
10. **Reconcile the four documentation defects in §4.6** — `FIRST_PAPER_RUN.md` first, because
    it is the page read before the open and it currently argues against #159.

Items 1 and 2 are what stand between this commit and a day 5 that can be believed. Item 3 is
what stands between it and a day 5 that can be believed *twice*, because the first restart
brings the hole back with it.

---

## 9. What this check did not cover

Stated so the absence is a recorded judgement rather than an oversight.

- **The integration and e2e suites were not run** — they need Postgres and Redis, and no daemon
  was available here. `tests/integration/test_kill_switch.py`'s five tests would not have run
  anyway (§6).
- **The web half of `lint` and `typecheck` did not execute** — `apps/web/node_modules` is absent
  and the global `tsc` is a major version ahead of the pin. Nothing is known about web
  formatting or types from this check.
- **No live or paper endpoint was contacted.** Everything about the venue's current state is a
  question for §7.1, not an answer in this document.
- **The reconciliation branch in §2b.2 is unverifiable from the tree.** Whether five unattended
  sessions produce a clean reconcile or a quarantine depends on what Alpaca did, and only
  `status.py` can say.
- **AUDIT.md's own counts are stale** — its header records `pytest tests/unit` at 2,161 test
  functions against the 3,047 measured here, and its state review is dated 2026-09-07. Findings
  #1, #5, #6, #9, #12 and #13 all bear on day 5 and are cited above from the tree rather than
  from the record; the record itself needs a refresh before it is quoted.
- **F13's bar-write amplification and the corporate-action quarantine were not traced.**
  `data/stream.py`'s `_handle_bar` still upserts per bar, which at `1m` × 20 symbols is ~7,800
  single-row round trips a session and is the reason `data.bars.upserted` floods the log an
  operator has to grep. `_report_adjustment` still writes back an adjustment it has just
  declared inconsistent (`scheduler.py:608`, *"Written back whether or not one factor explains
  it"*), with no quarantine and nothing stopping warmup reading that series.
- **The halt-clearing and dashboard-resume paths remain untested in production** — day-3 F4 and
  F6, both still "Untested" on day 4's scorecard. Day 5 is markedly more likely than day 4 to
  need them (§3.5, §2b.2), and nothing here exercised either.
