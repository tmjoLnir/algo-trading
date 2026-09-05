# Day 1 Paper-Week Fixes — Verification Audit

**Repository:** `tmjoLnir/algo-trading` · **Commit audited:** `027e1f6` · **Date:** 2026-09-05
**Scope:** every item in `docs/paper-week/day-1-review.md` — B1, B2 and F1–F14 — against the code
that claims to fix them, across PRs #134–#139.

> **Status.** Items **1 and 2** of §8's order of work are fixed — §3.4 with §3.4a, and
> §3.3. Items 3 and 4 (§3.2 and §3.1/§3.1a) are **open and deliberately deferred**: the
> investigation for them found the audit's own prescriptions unsafe as written, and the
> two are one change to the risk chain rather than two, so they are not a diff to rush.
> See the notes under those sections' headings. Everything in §4 onward is open. The
> sections below are left as they were written; where a fix corrected the audit, the
> correction is recorded at the section rather than by editing the finding.
>
> **§3.4a's prescription was wrong and was not implemented.** "Gate the recovery branch
> on `verdict.market_open`" fixes the closing-bell all-clear by moving it to the *opening*
> bell: `evaluate`'s baseline is floored at the session open, deliberately, so a feed dead
> since yesterday reads as non-stale for the first `max_silence_seconds` of every morning.
> An all-clear there reaches the operator at 09:30 — where they actually read it — and is
> followed by a CRITICAL two minutes later for a feed whose state never changed. The
> verdict now carries `data_is_current`, which is true only when a witness about the
> *data* (a message received, or a bar in storage) is timestamped at or after this
> session's open; recovery reads that, the close re-arms silently, and
> `connected_since` — the process's birthday, which F7 already demoted — cannot satisfy
> it.
>
> **§3.3 was implemented with the flag kept.** Deleting `--timeframe` outright would
> remove the only read-only way to ask "would `1d` fit?", which is one of the two ways out
> of the `risk_pct` sizing §2a says a minute series cannot carry. The flag now defaults to
> `None` and the saved row answers; an override is folded into the config so one process
> holds one timeframe, and announces itself as a what-if on its own `timeframe` check line
> rather than passing as a verdict about the saved configuration. The series is named in
> every history and sizing verdict, and in the `backfill_bars.py` fix lines — whose own
> `--timeframe` still defaults to `1d`, so advice given without it repaired a series
> nothing trades (§4.12).

**Method.** Each finding was read from the review, then traced in the tree at HEAD by an
independent verifier, then attacked by a second agent asked to refute the first. The
highest-severity conclusions were reproduced by execution rather than by reading. Build gates
were run: `make lint`, `make typecheck`, and the full unit suite.

---

## 1. The verdict

**Fourteen of the sixteen findings have real, well-argued fixes in the tree.** The engineering
quality is high — the docstrings carry the reasoning, the failure paths are considered, and
several fixes correctly refused to do what the review asked once the code turned out to
disagree with it (F10's rollover is the clearest example).

**But the review's own status block over-claims, and the gap is not cosmetic.** §7 says the
four P0 blockers are fixed; three of the four have a hole the fix did not close. Two of those
holes are new — introduced *by* the fixes — and one of them lets an order through the kill
switch.

**Recommendation: do not start day 2 on this commit.** Seven items below are pre-open work, and
three of them are one-line or near-one-line changes.

### Build gates

| Gate | Result |
|---|---|
| `make lint` (ruff + eslint + prettier) | **pass** |
| `make typecheck` (mypy 234 files, `tsc --noEmit`) | **pass** |
| `pytest tests/unit` | **2,728 passed in 99s** |

One flake: `test_alpaca_provider.py::TestRateLimiting::test_requests_are_spaced_when_an_interval_is_set`
failed on the first run under container load and passed on three subsequent runs. It asserts
that a sleep was *requested*, which is only true when the paced work finishes faster than the
interval. Pre-existing (PR #89), unrelated to the day-1 fixes.

---

## 2. Status of every finding

| | Finding | Review says | Actual |
|---|---|---|---|
| **B1** | Runner reads a bar series nothing writes | fixed | **partial** — wiring done; stream hard-codes `M1`, status blob drops the field |
| **B2** | Market entry into a flat symbol cannot be priced | fixed | **fixed** |
| **F1** | Strategy loop is unobservable | fixed | **fixed** (code half; token is a host action) |
| **F2** | Engine-side stop fallback unreachable | fixed | **partial** — a second entry drops the gap flag |
| **F3** | Kill switch has no exit carve-out | fixed | **partial** — the reversal guard reads the projected book |
| **F4** | Worker never reads halt state at boot | fixed | **fixed** |
| **F5** | Six minutes of data lost, reported as recovered | fixed | **partial** — a quote can still shrink a gap |
| **F6** | Crashes were self-inflicted | fixed | **partial** — REST half untouched; flap loop unthrottled |
| **F7** | Crash-looping worker cannot halt itself | fixed | **partial** — the recovery alert is not wired |
| **F8** | Nothing repeated the halt for 2h37m | fixed | **partial** — the reminder has no first run |
| **F9** | `halt.py` clears with no password, no audit | fixed | **fixed** (with a new third caller, below) |
| **F10** | Three scheduled jobs are stubs | fixed (#138, #139) | **fixed** |
| **F11** | Sizing not survivable on the intended timeframe | fixed | **partial** — preflight prices off the wrong series |
| **F12** | Mid-session restart for a no-op config change | fixed | **fixed** |
| **F13** | Feed is structurally thin | fixed | **fixed** — ADR 0026 |
| **F14** | `no_action` inflates the rejection counter | fixed | **fixed** |

---

## 2a. Would day 2 actually trade?

**Day 2 is Tuesday 2026-09-08, not Monday.** Monday the 7th is Labor Day — verified against
`TradingCalendar` at HEAD, which correctly returns no session for it. `TradingHoursRule` (rule 2
of 9) refuses everything before then and the runner sleeps to the next open.

**And on Tuesday: no, not on the current sizing row.**

The plumbing to the runner does hold at the default: `WorkerConfig.timeframe` is `1m`, the
migration backfills `'1m'` (not `'1d'` — the right choice, `'1d'` would have preserved the bug),
the stream writes `M1`, and the runner reads `config.bar_timeframe`. So `on_bar` *will* be
called, which day 1 never managed.

Signals should now be generated: `LiveContext.closes` serves the runner's series regardless of
what the strategy asks for, so the `1d` in `SmaCrossover`'s params does not starve it (§3.0). The
platform will run a 20/50 crossover on *minute* bars, which is a different strategy from the one
the parameters name — worth deciding deliberately, but not a blocker.

**The orders are the blocker: the sizing row cannot produce a passing order.**
`risk_pct` sizes by the distance to the stop, and a 2×ATR stop on a minute bar is cents wide:

```
equity 100,000   price 550   max_position_pct 0.10
on the 1-minute series, 2xATR stop = $0.40 wide

  risk_pct        0.01      -> FAIL  2500 shares = 1375.0% of equity
  risk_pct        0.0015    -> FAIL   375 shares =  206.2% of equity   <- day 1's revision 4
  risk_pct        0.0001    -> FAIL    25 shares =   13.8% of equity   <- one basis point
  equity_pct      0.05      -> PASS     9 shares =    5.0% of equity
  fixed_qty       10        -> PASS    10 shares =    5.5% of equity
  fixed_notional  5000      -> PASS     9 shares =    5.0% of equity
```

`risk_pct` is unusable on a minute series at any sensible value — even one basis point breaches
the 10% position cap. Anything that fits is below `MIN_USEFUL_SIZING_VALUE`, which
`preflight._sizing_fix` itself calls "too small to be a real setting".

So day 2 produces zero orders for a *different* reason than day 1, with the identical symptom —
and because of §3.3, `make preflight` reports PASS.

**Before day 2:** switch the sizing method to `fixed_qty` / `equity_pct` / `fixed_notional`, or
re-decide the timeframe. That second option is F11's own conclusion, resolves §3.0 at the same
time, and is still the live decision.

---

## 3. Blocking — fix before day 2

### 3.0 The strategy's declared timeframe is not the one it runs `medium`

**Not a zero-signal bug — this section previously said it was, and that was wrong.**

`SmaCrossover.on_bar` resolves its own timeframe from its own params, defaulting to `"1d"`
(`strategy/examples/sma_crossover.py:58`), and `trading.py:181` builds the strategy from
`config.strategy_params` alone, so nothing injects `config.timeframe`. But `LiveContext.closes`
**ignores the timeframe it is handed** and serves the one series the runner holds
(`runner.py:236-239`), after logging `runner.timeframe_mismatch` once per timeframe
(`runner.py:_check_timeframe`).

So the strategy is served minute bars and *will* generate signals. The runner's own docstring
names the residual precisely:

> "a strategy carrying `timeframe: 1d` — which is `SmaCrossover`'s own default — gets minute
> closes and computes a 20/50 crossover nobody asked for. That is the same class of silent
> divergence as the runner and the ingestor disagreeing, one layer up, and it is the half that
> survived fixing the wiring."

This is already known, deliberately a warning rather than a raise (raising inside `on_bar` would
turn a long-standing wrong parameter into an outage after three consecutive errors), and covered
by three tests at `tests/unit/test_strategy_runner.py:1875-1906`. Credit where due: the fix
anticipated this.

What remains is real but narrower than a blocker:

- **The declared parameters do not describe what runs.** `strategy_params` says `1d`; the
  strategy computes on `1m`. Anyone reading the config, the audit row or the dashboard is told
  the wrong thing about what the platform is doing.
- **`BacktestContext.closes` (`backtest/engine.py:400`) also ignores the timeframe**, serving
  whatever series the backtest was loaded with. So the two agree *by both ignoring the argument*
  rather than by honouring it — which holds only while every backtest is run on the same series
  as the live worker. Nothing enforces that.
- **Day 2 will run a 20/50 crossover on minute bars** — a materially different strategy from the
  daily crossover the parameters name, and not the one any backtest was necessarily evaluated on.
  That is a decision to take deliberately, not by default.

**Fix.** Inject the configured timeframe into the strategy's params at construction so the
declared value and the served value cannot differ, and decide explicitly whether the paper week
is testing a minute-bar or daily-bar crossover.

### 3.0b The runner takes one bar per pass, which was safe only at D1 `high`

`StrategyRunner._refresh_bars` (`apps/worker/src/atp_worker/runner.py:861`) asks
`get_last_n_bars(symbol, self.timeframe, 1)` — exactly one bar per symbol per pass — and appends
it only when it is newer than the last one held.

At `D1`, polled every 60 seconds, that is correct by construction: at most one bar exists per
day. At `1m` it is not. Any pass in which more than one bar has landed sees only the newest and
**silently discards the intermediate ones**, so the strategy's series acquires holes and its SMA
is computed across them.

Two triggers, both real for the paper week:

- **The reconnect backfill.** F5's own fix writes the whole outage window into storage in one
  shot (`stream.py:429-445` → `backfill_bars` → `upsert_bars`). Day 1's 18:45–18:51 hole would
  arrive as ~8 bars per symbol at once; the runner would take one.
- **Any pass that runs late.** `engine_tick_interval_seconds` defaults to 60 against a 60-second
  bar, so there is no margin at all — a pass delayed by the router, the venue or the database
  skips a bar.

**Fix.** Fetch by timestamp — everything newer than `held[-1].ts` — rather than by count.

### 3.0c The published status blob drops `timeframe` `medium`

`encode_running` (`libs/core/src/atp_core/persistence/worker_status.py:57-80`) lists ten config
fields and `timeframe` is not among them, so `decode_running` reconstructs a `WorkerConfig` where
the dataclass default `"1m"` applies.

The dashboard's running-worker panel therefore reports `1m` for every worker regardless of what
it is running. That panel exists to answer "is the running worker on the saved revision?", and
it cannot see a change to the one field whose mismatch caused day 1.

B1 threaded `timeframe` through the migration, the ORM, the save and load paths, the API view and
the dashboard form — and not through the status blob.

---


### 3.1 The halt carve-out approves orders that reverse a position `critical`

`KillSwitchRule` (`libs/core/src/atp_core/risk/rules.py:227-246`) permits an order that reduces
a holding and refuses one larger than the position — `order.qty > held`. But
`RiskEngine.validate` hands every rule the **projected** book (`risk/engine.py:148`), which adds
every in-flight *entry*. So `held` counts orders that have not filled.

Reproduced against HEAD, through the full nine-rule default chain:

```
settled position for SPY: flat
one working BUY 100 (submitted before the halt, unfilled)

  halted, SELL 100  -> APPROVED
  control, no pending entry, SELL 100  -> DENIED by kill_switch: trading is halted
```

And with a partially filled entry — CLAUDE.md §5's own "partial fills" warning:

```
settled position: long 40   (an entry for 100 filled 40; 60 still working)

  halted, SELL 40   -> DENIED by max_position_size
  halted, SELL 100  -> APPROVED   <- closes 40 and opens a SHORT 60, while halted
```

`docs/SAFETY.md:47-58` states the guarantee absolutely — "refuses every order except one that
can only make an existing holding smaller" — so the document is ahead of the code.

**Fix.** `KillSwitchRule` needs the settled book. Either exempt it from `project_pending`, or
pass both books to the chain and let this rule read the settled one. The rule's whole purpose is
to be the single decisive boolean; measuring it against a hypothetical is the one thing it
cannot afford.

#### 3.1a The carve-out removed a documented guard the platform implements *as* a halt

Worse than the projection bug, and independent of it. `OrderRouter._resolve_indeterminate`
(`execution/router.py:1078-1080`) engages a global `broker_unreachable` halt when an order's
outcome is unknown, and its docstring states exactly why:

> "It halts and does not flatten: halting stops new risk, while **flattening against a position
> that may not exist opens a short** (docs/RISK.md, 'Halting is not flattening')."

The halt was doing two jobs — stopping new risk *and* blocking a flatten against an unproven
position. F3's carve-out reasoned only about the first and removed the second. A flatten during
that halt is now approved, against a book the platform has just declared it cannot trust.

The same shape applies to a `reconciliation_mismatch` halt: the stored book is *known* to
disagree with the venue and the runbook forbids resyncing it, yet
`POST /positions/{symbol}/close` (`apps/api/src/atp_api/routers/positions.py:225`) builds its
flatten from that book and the carve-out now approves it.

**Fix.** The carve-out has to be conditional on the halt's `HaltReason` after all — or those two
reasons need their own refusal. The rule's "deliberately blind to `HaltReason`" argument holds
for `manual` and `data_feed_lost`; it does not hold for the two reasons that mean *the book
itself is untrustworthy*.

### 3.2 A partially filled entry blocks its own protective stop `high`

Halt-independent, and visible in the same trace above. `MaxPositionSizeRule`
(`risk/rules.py:260-283`) judges `abs(held + order.qty * sign)` against the projected book and —
unlike `DailyLossLimitRule` (`:348`) and `BuyingPowerRule` (`:448`) — has **no
`reduces_position` exemption**. So an exit sized off the settled position is refused because of
a fill that has not happened.

The runner's own comment on that path (`apps/worker/src/atp_worker/runner.py:983`) reads: *"The
stop triggered and the exit was refused, so the position is still on. Of the four refusals
recorded here this is the one most likely to cost money."*

Pre-existing in `project_pending`, but **unreachable until now**: day 1 submitted zero orders.
B1 and B2 together make it reachable for the first time.

**Fix.** Give `MaxPositionSizeRule` and `MaxExposureRule` the same `reduces_position` exemption
the other two rules already have. Refusing an exit to protect a position cap is the inversion
this codebase argues against everywhere else.

### 3.3 Preflight validates a different timeframe than the worker runs `high`

`scripts/preflight.py:77` declares `--timeframe` with `default="1d"` and reads it from **argv**,
never from `WorkerConfig.timeframe` — even though the config row is loaded ten lines earlier at
`:100`. B1 created one value so writer and reader cannot disagree; the command that answers
*"is this configuration ready to spend a week trading paper?"* does not read it.

Two checks are priced off that flag:

- **sizing** (`:351`, `:371`) — F11's entire fix
- **warmup history** (`:221`, `:257`) — bar counts measured on the wrong series

Demonstrated with day 1's own configuration (`risk_pct 0.0015`, worker timeframe `1m`):

```
preflight default --timeframe 1d  (ATR ~ $5.00)
   -> PASS: risk_pct sizes the first entry at 15 shares = 8.2% of equity, under the 10% cap

what the worker will actually run: 1m  (ATR ~ $0.20)
   -> FAIL: risk_pct asks for 375 shares at 550 = 206.2% of equity ... max_position_size
      would refuse every entry, and the week would look silent
```

F11's fix is sound; its wiring defeats it. An operator runs `make preflight`, sees green, and
gets another silent week — from a different cause than B1, with the same symptom.

**Fix.** Delete the flag's default (or the flag) and use `config.bar_timeframe`.

#### 3.3a Even with the right timeframe, it prices against a different ATR

`scripts/preflight.py:371` reads exactly `config.stop_period + 1` bars and computes the ATR over
that window. `StrategyRunner._atr` computes it over `self._bars[symbol]` — the whole warmed-up
series, 51+ bars at boot and growing all session — with the same period. Wilder smoothing has not
settled at 15 bars, so the two numbers differ, and so does the share count each produces.

The check's own docstring is explicit about the standard it is held to: *"Sized through
`position_size`, never re-derived: the number this predicts has to be the number the router
computes, or the prediction is about a different platform (ADR 0006)."* It honours that for the
sizing function and breaks it for the ATR feeding it.

Two smaller ones in the same check: it prices only `config.symbols[0]` while the history and
quote checks loop the whole watchlist (`:220`, `:231`, `:367`) without saying so; and it assumes
a flat book, handing only `equity=` to a rule that caps the position the order *leaves behind*.

No test can express any of this — every sizing test in `tests/unit/test_preflight.py` calls
`check_sizing_is_reachable` directly with hand-picked prices, including the one carrying F11's
name at `:205`.

### 3.4 The staleness recovery alert is never wired `high`

`apps/worker/src/atp_worker/main.py:261` constructs
`StalenessMonitor(config.max_silence_seconds, kill_switch=kill_switch)` with **no `alerts=`**.
The sink is built at `main.py:166` and passed to the kill switch (`:167`), the session watch
(`:327`) and the reconciler (`:406`) — just not here. `_announce_recovery` therefore returns at
`libs/core/src/atp_core/data/stream.py:693` (`if self._alerts is None: return`) and the all-clear
is a `WARNING` log line.

§7 claims: *"`data.staleness.recovered` also reaches a phone now, and says that the halt it
engaged is still engaged, because a CRITICAL followed by silence cannot be told from 'fixed
itself, waiting for you'."* It does not. `docs/OBSERVABILITY.md:100` says the same.

**Fix.** One line: `alerts=alerts` at `main.py:261`. Add a test asserting the wiring — nothing
currently exercises `main()`.

**But do not wire it without fixing §3.4a first**, or the first thing it sends is a lie.

#### 3.4a The all-clear fires at the closing bell, not on recovery

`StalenessMonitor.watch` treats *any* non-stale verdict as recovery (`stream.py:672`):

```python
elif not verdict.stale and self._alerted:
    self._alerted = False
    log.warning("data.staleness.recovered", msg="market data is flowing again — ...")
    self._announce_recovery()
```

and `evaluate` returns `stale=False` whenever the market is shut — `StalenessVerdict(stale=False,
market_open=False, reason="market is shut — silence is expected")`. Nothing checks
`verdict.market_open` before calling it recovery.

So a feed that dies at 14:00 and is **still dead** announces *"Market data is flowing again"* at
the closing bell, and resets `_alerted`, so the ongoing outage stops being tracked. Today that is
only a log line, because the sink is unwired. Fix §3.4 alone and it becomes a phone alert
contradicting the CRITICAL that preceded it — which is precisely the "fixed itself, waiting for
you" ambiguity F7 set out to remove.

**Fix.** Gate the recovery branch on `verdict.market_open`.

### 3.5 A flapping venue produces an unthrottled reconnect loop `high`

Both stream adapters put the backoff **and** the 900-second budget check inside the `except`
around `_open()`. When the server *accepts* a connection and drops it without delivering,
`_open()` does not raise, so the loop runs

```
_open() -> _receive() returns None -> break -> _open() -> ...
```

with no sleep, no `attempts` increment and no budget check. The `delivered` flag correctly stops
the budget being *reset* across a flap, but the code that reads it is never reached.

Both files carry a comment claiming this exact case is handled:

- `libs/core/src/atp_core/data/providers/alpaca.py:637-641`
- `libs/core/src/atp_core/brokers/alpaca.py:620-623`

Alpaca permits one stream connection per key, so the "connection-limit fight" both comments name
is the trigger, and the consequence is hammering the venue rather than backing off.

**And the `delivered` guard that was supposed to prevent this does not work.** Both loops reset
`attempts = 0` and `first_failure_at = None` on the first frame received:

```python
frame = await self._receive(connection)
if frame is None: break
if not delivered:
    delivered = True; attempts = 0; first_failure_at = None
```

`_receive` returns `self._parse_frame(raw)` for any frame it can parse, and a subscription
confirmation parses to `[]` — which is not `None`. **Alpaca always sends one after subscribe**
(the codebase defines it as `REAL_SUBSCRIPTION_MSG` and uses it in its own happy-path test at
`tests/unit/test_alpaca_realtime_feed.py:508`). So every connection satisfies `delivered`
immediately.

The comment above that block says resetting on connect alone "would otherwise restart the budget
on every loop and retry for ever". That is precisely what happens: accept → ack → drop is the
ordinary shape of a connection-limit fight, and it produces an unthrottled loop in which the
15-minute budget **can never expire** and the give-up path is never reached. Nothing alerts.

**The regression test cannot catch it.** `test_a_connection_that_never_delivers_keeps_backing_off`
(`tests/unit/test_alpaca_realtime_feed.py:624`) drives a fake that never sends the subscription
ack — the very frame defined 500 lines above it in the same file. This is the FakeBarRepo shape
the review itself identified in F5, repeated.

**Fix.** Move the throttle and budget check out of the `except` and onto the flap path: a
connection that closes before delivering *market data* is a failed attempt, and a protocol ack is
not delivery.

---

## 4. Should fix this week

### 4.1 B1's single value does not reach the writer `high`

`libs/core/src/atp_core/data/providers/alpaca.py:869` stamps every streamed bar
`timeframe=Timeframe.M1`, unconditionally. `StreamIngestor._handle_bar` (`stream.py:303-306`)
upserts the bar exactly as parsed, so `bar_timeframe` controls what the ingestor **reads**
(`:245`) and what the reconnect backfill **writes** (`:437`) — but not what the live stream
writes.

The default is `1m`, so day 2 is safe. But B1's fix added a dashboard dropdown with seven
options, and picking any of the other six recreates day 1 exactly: bars written at `1m`, runner
reading something else, `just_closed` empty, no error anywhere. Worse, the backfill path *does*
honour the setting, so a `5m` configuration writes a mix of both series.

Two of those seven (`1h`, `4h`) are not in `SUPPORTED_TIMEFRAMES` (`data/gaps.py:60`) at all, so
they additionally get no nightly gap sweep (`scheduler.py:316`) and no corporate-action refresh
(`scheduler.py:491`), both silently. `WorkerConfig._check_timeframe` (`config.py:424`) validates
only the name vocabulary.

**Fix.** Either stamp the streamed bar from the configured timeframe, or reduce the dropdown to
what the platform can service and validate against `SUPPORTED_TIMEFRAMES`. The review also asked
for a boot-time check that the newest stored bar in the runner's timeframe is younger than one
bar interval during RTH; that check does not exist (`check_warmup` takes `newest` and only
prints it).

### 4.2 F2's protection gap is forgotten by the next entry `high`

`_stop_is_missing` (`runner.py:1062-1070`) is gated on membership in `self._unprotected`, which
`_protect` pops whenever a call reports `result.is_fully_protected` (`:1413-1417`). But
`ProtectionResult.unprotected_qty` measures **only the increment this call covered**
(`execution/router.py:556`, `unprotected_qty=increment - covered`). A second entry into a symbol
whose earlier protective stop was refused therefore reports full protection, the flag is
dropped, and the position is again unprotected at the venue *and* unwatched by the engine — the
finding verbatim.

Two related residuals:

- **Across a restart**, `_unprotected` and the router's protected-quantity map are both
  per-process, so a position whose stop was refused returns to the "unknown" state and the engine
  declines to watch it. Documented as deliberate, but day 1 had three restarts in 158 seconds.
- **Nothing retries a refused protective stop.** `submit_protective_orders` is called only from
  `_protect`, which runs only on a growing fill (`runner.py:1408`), so the engine-side watch is
  the sole recovery rather than defence in depth.
- On the partially-covered branch the runner calls `router.flatten` (`runner.py:974`), closing
  the **whole** position, in direct contradiction of `submit_protective_orders`' documented
  caller contract #2 (`router.py:405-409`): close only over
  `abs(position.qty) - broker_side_protected_qty(...)`, because closing the covered part again
  opens a reversed position with nothing on it.

#### 4.2a The membership gate short-circuits before the router is ever asked

Larger than the second-entry case above, and needing neither a restart nor a second entry.
`_stop_is_missing` (`runner.py:1067`) returns `False` whenever the symbol is absent from
`_unprotected` — *before* it asks the router anything:

```python
if position.symbol not in self._unprotected:
    return False
covered = self.router.broker_side_protected_qty(position.symbol, position)
```

`_unprotected` is written only when `submit_protective_orders` comes back short. So a stop that
was **accepted** and later cancelled, rejected or expired at the venue never enters the map, and
the engine never looks — the position is unprotected at the venue and unwatched by the engine,
which is F2 verbatim. The method's own docstring describes "known covered — protection was armed
and the router still counts enough working quantity against the position", but that check is
exactly what the gate skips.

The converse also holds: once *any* refusal is recorded for a symbol,
`broker_side_protected_qty` counts only stops **this process instance** placed
(`router.py:226, 778-785`), so a stop resting from before a restart reads as absent and the
engine flattens a position the venue is also stopping — a double exit in the direction the fix
exists to prevent.

**Fix.** Drop the membership gate and let `_stop_is_missing` do the arithmetic it already
performs two lines later, against the whole position rather than the increment.

### 4.2b B2's mark can now refuse the protective stop it enables `medium`

B2 is the right fix and the entry-pricing half is sound. But marking a flat symbol has a second
effect the finding did not consider: `_protects` (`execution/router.py:_protects`) judges whether
a proposed stop is still a stop by comparing it against `position.last_price` — the very field
`_mark` now populates from `quote.mid` (`runner.py:917`).

Before B2, a flat symbol had **no** mark, so `_protects` returned True ("unmarked, we cannot
judge") and the stop was armed. After B2 it can return False. When it does,
`submit_protective_orders` sets `stop_level = None`, `position.stop_loss_price` is never armed,
and the result is **no broker-side stop and no engine-side level** — logged CRITICAL as
`order.position_unprotected` (`router.py:499-506`), and invisible to
`StopManager.should_trigger`, which returns False on a null level.

The entry is priced off the quote mid and the stop off an ATR computed over bar closes, so this
fires when the quote has moved more than 2×ATR from the bar the ATR was derived on, between that
close and the protection submit. On minute bars in a fast open that is not exotic.

Two smaller ones from the same fix:

- **The out-of-watchlist branch is inert.** `wanted` includes holdings that have left the
  watchlist (`runner.py:908`), but neither price source can serve them: the quote cache holds
  only subscribed symbols, and `_context.last_price` reads `self._bars`, which holds only
  watchlist symbols. So the symbol the comment calls "the one symbol this cannot skip" is still
  the one that goes unmarked.
- **`check_sizing_is_reachable` no longer prices the entry the way the router does**
  (`scripts/preflight.py:380`) — a second route to the same silent week §3.3 describes.

### 4.2c The session summary reports week-to-date totals as the day's `high`

`RunnerStats()` is constructed **once**, at `apps/worker/src/atp_worker/runner.py:368`. `run()`
re-runs `warmup` at every market open, and warmup resets only `stats.started_at` (`:501`) —
`evaluations`, `signals_generated`, `orders_submitted` and `orders_rejected_by_risk` are never
zeroed anywhere in the tree. The compose worker is `restart: unless-stopped` with no daily
restart.

So from day 2 of the paper week onward:

- `runner.evaluated`'s `submitted` and `refused` fields are week-to-date, not pass- or
  day-relative.
- **`summarise_the_session` reports the same cumulative counters as "what the day actually did"**
  (`scheduler.py:180-186`). That message is F8's headline deliverable — "the one message worth
  sending when *nothing* happened, because nothing happening is indistinguishable from working
  perfectly until somebody says so". On day 3 it will say "4 orders submitted" about a day that
  submitted none.

**Fix.** Zero the per-session counters in `warmup`, or have the summary diff against a snapshot
taken at the open.

### 4.2d A pass that raises after `_refresh_bars` drops the bar permanently `high`

`_refresh_bars` (`runner.py:861-870`) appends the newly closed bar to `self._bars[symbol]` and
returns it. If any later step of the same pass raises — `_mark` on a Redis blip in
`quote_cache.get_quotes`, or `_record_signal`/`_persist` on Postgres, which `runner.py:1231`
deliberately allows to raise — `evaluate()` catches it and logs `runner.evaluation_failed`, and
the bar is already in `self._bars`. The next pass compares `bar.ts <= held[-1].ts` and skips it.

**The strategy is never given that bar**, and the next pass's `runner.evaluated` line reports a
completely healthy pass. The heartbeat F1 added cannot express the one thing that went wrong.

**Fix.** Append to `self._bars` only after the pass that consumes the bar has completed, or track
a high-water mark of bars actually handed to `on_bar`.

### 4.2e The heartbeat's freshness fields hide a single dead symbol `medium`

`newest_bar_at` and `newest_bar_age_seconds` are a `max()` across the whole watchlist
(`runner.py:764-767`), and `symbols_held` counts any symbol holding even a stale warmup bar. On
the 20-symbol watchlist this platform runs, one symbol whose feed dies is invisible: the other 19
keep the max fresh, so `newest_bar_age_seconds` stays ~30s for ever.

### 4.2f F4 fixed the log line and left the screen — and its new text is false `high`

Two defects, and the first is two fixes from this same review contradicting each other.

**The strings F4 added are not true.** `main.py:392` sets
`msg="HALTED — no order will reach the venue"` and `:398` sets
`effect="every order will be refused by kill_switch until a human clears it"`. Neither has held
since **PR #136**, one PR earlier in this same series, gave `KillSwitchRule` its exit carve-out:
a reducing order *is* permitted through a halt, by design. F4 (PR #137) asserts a guarantee F3
(PR #136) had already removed.

**The published status blob still carries the day-1 sentence.** `main.py:353-362` writes
`trading=decision.enabled` and `reason=decision.reason` — the unqualified *"trading sma_crossover
with paper money"* — **25 lines before** the halt is read at `:379`, and never revisits it. The
log line now says `halted=True trading=False`; the dashboard's running-worker panel, which is
where an operator actually looks, still says `trading: true` with day 1's exact wording, under a
seven-day TTL.

F4's finding was *"the worker announced 'trading sma_crossover with paper money' three times while
it was engaged"*. The log stopped saying it. The screen did not.

**Fix.** Read the halts before publishing the status, fold `halted` into `RunningWorkerConfig`,
and correct both strings to say what the carve-out actually permits.

### 4.3 F6's REST half was never touched `medium`

The finding named two budgets. Only the WebSocket one was changed. `brokers/alpaca.py:74` and
`data/providers/alpaca.py:68` are both still `_MAX_ATTEMPTS = 5` with un-jittered `2**attempt`
backoff (≈31s of sleep) and no elapsed-time bound — which is the ~67s that `GET /v2/positions`
gave up in on day 1.

The process no longer dies from it (a failed scheduler job now reschedules rather than going
dormant), so the crash loop is broken indirectly. But §7's *"Both streams are now bounded by
elapsed time"* silently re-scopes the finding's *"both retry budgets"*, and nothing in `docs/`
records that the REST ladder is still five attempts.

### 4.4 A quote can still shrink a bar gap, and silence a watchdog `medium`

`StreamIngestor._dispatch` sets `stats.last_message_at` at `stream.py:274` — *before* the
Quote/Bar/Trade branch — so **a quote advances it**. That value is then used as evidence about
*bars* in two places:

- the reconnect backfill window, `max(storage_watermark, last_message_at)` at `stream.py:394-400`
- the staleness watchdog's baseline at `stream.py:622-630`

A feed delivering quotes but no bars keeps both nets quiet. The watchdog's own docstring says
catching a "connected and frozen" feed is the only reason it exists. Day 1's outage killed both
streams together, which is why this never showed.

The behaviour is deliberate and tested — `test_a_stale_watermark_does_not_widen_a_short_blip`
(`tests/unit/test_stream_ingestor.py:601`) pins it — but the test uses a **bar** as the last
message, so the quote case is untested, and `docs/DATA.md:120` states the guarantee
unconditionally: "a restart cannot shrink a gap any more."

Related, from the same fix: the ingestor reads the watermark at startup and never backfills on
that path (`stream.py:186`), so a worker restarted mid-outage that connects cleanly on the first
attempt never emits `FeedReconnected` and never closes the hole it inherited. Day 1's hole was
refetched at all only because worker #4's own connect flapped.

### 4.4b A zero-bar backfill counts as recovery, and refreshes the watchdog `high`

`_backfill_gap` returns `result.bars_written` (`stream.py:475`), which is **0** when the provider
had no data for the window, and `None` only on a `DataError`. `_on_reconnect` returns early only
on `None` (`stream.py:325`), so a backfill that recovered **nothing** is treated as success and
advances the watermark:

```python
self.stats.storage_watermark = event.reconnected_at   # stream.py:335
```

`StalenessMonitor.evaluate` reads `storage_watermark` as a peer witness (`stream.py:622-629`). So
during a venue-wide outage — where the historical endpoint has no data for the window either,
which is exactly day 1's seven minutes — every flap refreshes the watchdog's baseline and **the
watchdog never fires**.

`BackfillResult.ok` (`data/backfill.py:96-98`, "True when every symbol returned data for every
window asked for") exists for precisely this question and is never consulted. The comment three
lines above the assignment says the watermark must not move because "claiming data is good up to
now is exactly the false 'recovered' this whole change is about" — and then it moves it on a
backfill that recovered nothing.

This is F5's fix defeating F7's watchdog: the two halves of the same PR, in opposite directions.

**Fix.** Gate the watermark advance on `result.ok`, not on `bars_written is not None`.

### 4.5 The halt reminder has no first run `medium`

`next_due` for an interval trigger returns `now + 15 minutes` (`scheduler.py:827`) with no run at
scheduler start. A worker that restarts and dies inside fifteen minutes emits no reminder at all.
§7's claim that *"the crash loop that destroyed the old per-process state cannot suppress it"* is
true of the **state** — which is correctly in Redis — and false of the **trigger**. Day 1's three
deaths spanned 158 seconds.

Worse, the death alert only covers responsibilities that have already **started**
(`main.py:192`). A boot-path failure exits the process with no alert at all, and in that crash
loop `run_scheduler` is never reached — so the halt reminder can never fire at *any* restart
period, not only a sub-fifteen-minute one. That is F8's original silence, reproduced.

A restart in the last fifteen minutes of a session also pushes the reminder to the next open, and
a restart across the close drops that day's summary permanently: the scheduler never catches up a
missed session edge (`scheduler.py:832`).

The reminder is also `market_hours_only` (`scheduler.py:712`), so day 1's 2h37m halt would have
produced roughly four reminders rather than the "ten" the comment at `:710` asserts, and the last
83 minutes before the operator cleared it would still have been silent apart from the close
summary.

### 4.6 `KillSwitch.clear` now has a third caller that breaks its own contract `medium`

`libs/core/src/atp_core/risk/killswitch.py:246` still reads: *"Resume. **Requires a named human;
always audit-logged.**"* F10's `rollover_daily_counters` (`scheduler.py:637`) clears a daily-loss
halt with a job actor and **no audit row**. It is narrowly guarded and it alerts — but "a halt
cleared with no audit row" is true in the platform again, one PR after F9 fixed exactly that, and
the review's still-open list does not mention it.

**And the rollover clears whatever global halt is in force, not just the daily-loss one.**
`KillSwitch.engage` deduplicates on `(scope, target)` and returns the existing record
**unchanged** (`killswitch.py:213-216`) — it does not compare reasons. And **eight of the nine
automated engage sites in the platform use `HaltScope.GLOBAL`**: `data/stream.py:502` and `:720`,
`execution/router.py:1123`, `execution/reconciliation.py:146` and `:192`, `worker/main.py:561`,
`worker/runner.py:541` and `:553`. Only `runner.py:710` is strategy-scoped.

So a standing global `DAILY_LOSS_LIMIT` halt silently absorbs every later cause — a feed loss, a
reconciliation mismatch, an indeterminate order outcome — and the record still reads
`DAILY_LOSS_LIMIT`, `engaged_by=daily_loss_limit`. The next morning `rollover_daily_counters`
matches all three of its guards and **releases a halt that a live outage, a book mismatch or an
unknown-outcome order is also depending on**. `StaleDataRule` still refuses orders on quote age,
so nothing trades on stale prices — but the banner clears and the operator is told trading
resumed.

The test meant to bound the rollover's blast radius (`tests/unit/test_worker_scheduler.py:669`)
seeds two `HaltRecord`s that both carry `scope=GLOBAL, target=None` into the fake — a state
`RedisKillSwitch` cannot produce, since that is one key — and asserts on call count rather than
on which halt survived. Same shape as the `FakeBarRepo` hole the review found in F5.

### 4.7 `realised_pnl` is sign-inverted `medium`

`libs/core/src/atp_core/analytics/daily.py:155` computes
`Σ(avg_fill_price × filled_qty × side.sign)`, and `Side.sign` is **+1 for BUY**
(`domain/enums.py:31`). A buy is cash *out*, so the sum has the wrong sign. Demonstrated:

```
round trip: BUY 10 @ 100, SELL 10 @ 110   (true realised P&L = +100)
  DailyReport.realised_pnl = -100
```

`Position.apply_fill` uses the same expression correctly, with a minus:
`cash -= qty * price * sign + fee`. The report's version also excludes fees and counts entries
that never closed, so it is signed gross cash flow with the wrong sign, labelled P&L.

It is `Decimal` (§1.1 honoured) and exported from `atp_core.analytics`, but rendered nowhere and
covered by no test — a wrong number waiting for its first reader.

### 4.8 The daily report has no P&L section, and does not say so `medium`

`summarise()` builds four sections (`daily.py:162-167`): trades, risk rejections, halts, feed
incidents. Neither caller passes equity — not the worker (`scheduler.py:417`) nor the API
(`analytics.py:805`) — so P&L is silently missing rather than reported **absent**. That is
exactly the failure the module's three-valued design was written to prevent, applied to the
number that matters most on a trading platform. `render()` prints an `equity` line only when both
figures are present, so nothing on the page says the section is not there.

`PortfolioRepository` holds the snapshots; the module docstring already names it as the source.

### 4.9 The rollover's "not a human" guard is string equality against an unvalidated flag `medium`

`rollover_daily_counters` releases a halt only when `record.engaged_by == DAILY_LOSS_RULE`
(`scheduler.py:628`), and its docstring says this exists so that "a human who happened to pick
that reason from `scripts/halt.py --reason`" is untouched. But `--by` is free text
(`scripts/halt.py:196`) and `--reason` accepts every `HaltReason` including the automated ones
(`:200-203`, help: "the automated reasons are for the code that detects them" — advice, not
enforcement).

So `halt.py engage --by daily_loss_limit --reason daily_loss_limit --scope global` produces a
human halt that the platform clears automatically the next morning, with no password and no
audit row.

### 4.10 The daily-loss halt engages on the next refused *entry*, not on the breach `medium`

`_escalate` is reached from one site — `runner.py:1137-1139`, inside the "order was refused"
branch. A session that has breached its daily loss limit and then stops emitting entry signals is
over its limit with no halt, no alert and no banner.

### 4.10b The daily-loss halt cannot engage in the configuration the week runs `high`

`_escalate` branches on `decision.rule` (`runner.py:539`). But `RiskEngine.validate` returns on
the **first** denial (`engine.py:151-159`), and `DailyLossLimitRule` is **8th of 9** in
`default_rules` (`engine.py:240-250`) — behind `MaxPositionSizeRule` (5th), `MaxExposureRule`
(6th) and `MaxOpenPositionsRule` (7th), none of which shrink an order; they deny it.

Combine that with §2a: on a minute series with `risk_pct` sizing, `MaxPositionSizeRule` denies
**every** entry. So during the paper week as configured, every order is refused at rule 5,
`DailyLossLimitRule` never runs, `_escalate` never sees `daily_loss_limit`, and **the daily-loss
halt F10 built cannot engage** — nor, therefore, can the rollover that clears it.

F10's headline was that `HaltReason.DAILY_LOSS_LIMIT` was "an enum value with no writer" and now
has one. It has a writer that the platform's own rule ordering prevents from being reached, in
the one configuration this week will run.

### 4.11 F9's residual is no longer theoretical `medium`

The review's own note — *"The risk layer's own automated triggers still write no row, which is
the remaining half of the gap and is not this change"* — was written when nothing in the risk
layer engaged a halt. F10's `StrategyRunner._escalate` now writes `DAILY_LOSS_LIMIT` and
`RATE_LIMIT_STORM` for the first time. There are nine automated engage sites and none of them
audits.

---

### 4.12 The nightly sweep will probably not repair day 1's lost bars `high`

The review lists *"`backfill_missing_bars` still has to run to repair the ~108 bars day 1 lost"*
as an open operational item. It will likely not repair them.

`expected_windows` (`libs/core/src/atp_core/data/gaps.py:137-143`) yields **every** slot in the
session for an intraday timeframe — one bar per symbol per minute. On IEX that is the wrong
expectation by construction: ADR 0026 records that 12.4% of minutes have no print and therefore
no bar, and the historical REST path sends `feed=iex` too (`providers/alpaca.py:308`), so those
windows are unfillable.

The sweep then takes `gaps[:max_gaps_per_symbol]` — **chronologically first**
(`data/backfill.py:314`, budget 50). With roughly 48 phantom gaps per symbol per day over the
seven-day lookback, the budget is consumed by the oldest windows and the 18:45–18:51 outage this
review is about is what gets dropped, with a `data.backfill.gaps_truncated` warning and nothing
else.

ADR 0026 line 116 names exactly this — "treat missing minutes as gaps and backfill them from
REST" — as "the worst of the options", while the shipped nightly cron does it.

**And the repair tool has the same defect as preflight.** `scripts/backfill_bars.py:51` declares
`--timeframe` with `default="1d"`. An operator repairing day 1's *minute* bars who forgets the
flag silently backfills a series nothing trades — on the one command the review's own open item
depends on.

**Fix.** Either measure intraday gaps against a per-symbol expectation, exempt intraday series
from the sweep, or repair day 1's window with an explicit ranged `scripts/backfill_bars.py
--timeframe 1m` run and stop relying on the sweep for it. Default both scripts' flag from
`WorkerConfig.timeframe`.

### 4.12b F12's no-op warning diffs against the wrong baseline `high`

`worker_config.unchanged` (`apps/api/src/atp_api/routers/worker.py:586`) compares the new
revision against the previously **saved** row, not against what the **running** worker loaded.
The runbook mandates deferring restarts during a session, so those two routinely differ.

Sequence: the worker is running revision 4. An operator saves revision 5 changing the sizing, and
correctly does not restart. They save revision 6 with the same values — a re-click, or a second
edit that lands on the same numbers. The diff against revision 5 is empty, so the endpoint logs
*"this revision changed no field — a restart to pick it up would cost a session interruption for
nothing"*. The running worker is still on revision 4 and genuinely needs that restart.

The message is F12's whole deliverable, and it can invert.

### 4.13 F14 was fixed with a different condition than the one specified `medium`

The finding's fix line is verbatim: *"Gate the counter on `result.decision.approved` being False,
not on `submitted`."* `runner.py:1137` gates on `result.decision.rule != NO_ACTION` instead. The
two are not equivalent — `approved and not submitted` is reachable from three router paths.

So a **venue** rejection (approved by risk, refused by the broker) still increments
`orders_rejected_by_risk`, the counter whose stated purpose (`router.py:117-119`) is telling an
operator whether the *risk config* is too tight, and is logged as `runner.signal_refused` with
`rule=` and `reason=` both empty.

Worse, `_escalate` is then called with an approved decision, falls through both branches, and
hits `self._rate_limit_refusals = 0` (`runner.py:565`). A broker rejection interleaved among
rate-limit refusals therefore suppresses the `RATE_LIMIT_STORM` halt the same PR built.

The symptom the finding named — an EXIT against an already-flat position — *is* fixed.

### 4.14 ADR 0026 is not enforced, and its reversal path is wrong `low`

- No preflight check asserts the feed is `iex`, so the ADR's "never switch during an evaluation
  period" is honour-system only. Every other configuration decision in the platform has a check.
- The ADR says "switching to `sip` is one environment variable" (line 84), but
  `alpaca_stream_url` hardcodes `/v2/iex` (`config.py:39`) and the mismatch is mislogged as
  `feed=sip` (`providers/alpaca.py:742`). `AUDIT.md` #43 has this open at HEAD.
- The ADR is cited from nowhere but itself and the day-1 review — not from `config.py:39-40`,
  `.env.example:63-65` or `docs/DATA.md:12`, which are the three places an engineer touches the
  feed.

---

## 5. Documentation defects

`docs/paper-week/day-1-review.md` is the record for this work, and it is now inconsistent with
the tree in four places:

| Line | Says | Actually |
|---|---|---|
| 531, 566-567 | `apply_corporate_actions` and `generate_daily_report` "remain stubs" | both implemented in PR #139 |
| 607 | "The daily report is still a stub" | implemented in PR #139 |
| 584-588 | P0 items 1–4 carry no `~~…~~ **Done**`, unlike every P1 and P2 item | §7's prose above says all four are fixed |
| 456, 512 vs 560 | "`METRICS_TOKEN` … still has to be set on the host" vs "It was set on the host after day 1" | self-contradiction inside one section |
| 454-459 | "The four P0 items are fixed … The rest of P1, and all of P2, are open" | contradicted by lines 593-617 of the same document, which mark F4–F8 and F10–F14 done |

Also:

- `apps/worker/src/atp_worker/scheduler.py:174` — `summarise_the_session`'s docstring still calls
  `generate_daily_report` "still a stub".
- `apps/worker/src/atp_worker/tasks.py:262-267` — `generate_report_task`'s docstring still says
  `/analytics/reports/daily` "is a stub for its own reasons"; it is implemented.
- **No ADR for PR #139's architectural decision.** Detecting corporate actions by comparing
  adjusted-close series rather than calling Alpaca's corporate-actions endpoint is a real
  architectural choice with a stated trade-off. CLAUDE.md §6 requires an ADR; the reasoning lives
  only in a module docstring and a commit message. ADR 0017 covers adjusted closes in backtests,
  not this.

The roadmap, by contrast, is honest: PR #139 moved "Daily report" from *Unclaimed* to *Built and
unticked* rather than ticking it, on the correct grounds that Phase 5's *Verifiable:* line is
about the dashboard.

---

## 6. Test-coverage notes

The suite is large and mostly good — 2,728 unit tests, and several fixes ship the exact
regression test the finding implied (F6 replays a seven-minute outage; F5 has
`test_storage_widens_a_gap_the_feed_understates`; the F1 heartbeat has a B1-shaped case
asserting `bars_closed == 0` when runner and repo disagree).

**Three fixes can be reverted with the suite still green** — established by actually doing it:

| Fix | Revert | Result |
|---|---|---|
| **F2** | replace the only write to `self._unprotected` (`runner.py:1422`) with `pass` | **87 runner tests pass** |
| **F4** | delete the whole halt-at-boot block (`main.py:379-400`) | **22 worker-main tests pass** |
| **B1** | restore `Timeframe.D1` in `build_runner` in a form that dodges the literal string | only the `inspect.getsource` test fails; every behavioural test passes |

The F2 tests hand-set the private state (`runner._unprotected[SYMBOL] = Decimal("10")`) and then
assert `_exit_reason` acts on it, so the causal chain the fix *is* — a refused protective order is
remembered, and the engine then watches the level — is never joined. `grep -rn "worker.ready"
tests/` returns **zero hits**; all four `TestReadingTheHaltAtBoot` tests call private helpers, and
`test_it_reports_every_scope_not_just_global` asserts `_active_halts(switch) == switch.halts`, a
pass-through identity check on a fake, which tests nothing about scope despite its name.

The rest:

- **Nothing exercises `main()`.** F4's wiring, and F7's missing `alerts=`, are both in the boot
  path, and no test asserts either. That omission is precisely how §3.4 shipped.
- **B2 is only ever tested with a one-symbol watchlist** (`test_strategy_runner.py:403`), so
  "every symbol in `self.symbols` is marked" is never actually exercised across symbols.
- **No test drives the kill-switch carve-out through `RiskEngine.validate` with `pending`**,
  which is the only shape the live path uses — the gap that let §3.1 land. Every new kill-switch
  test calls `KillSwitchRule.check` directly with a settled `Portfolio` from the local helper
  (`tests/unit/test_risk_engine.py:64`), which is structurally incapable of expressing the book
  the engine actually hands the rule.
- **`FakeRouter.submit_protective_orders` hard-codes its coverage numbers**
  (`tests/unit/test_strategy_runner.py:320`), so the runner suite cannot express the failure F2's
  fix is about; the five tests that cover it assert on hand-set state rather than on state any
  real event produced.
- **No test covers preflight's timeframe wiring** — the test carrying F11's name tests only the
  fix string.
- **The audit-write-failure path is stubbed in all ten `halt.py` tests**, so the "best-effort,
  never blocks the act" guarantee `docs/RUNBOOK.md` depends on during a Postgres outage is
  unverified.

---

## 7. Still open by design, and host actions

Carried forward from the review and still true:

- **`METRICS_TOKEN` on the host** — a deployment secret no commit can discharge.
  `scripts/preflight.py` now warns on it (`check_metrics_token`), deliberately without failing.
- **`backfill_missing_bars` still has to run** to repair the ~108 bars day 1 lost.
- **Feed incidents have no store.** The daily report reports that section as *absent* rather than
  zero, which is the right call and worth keeping.
- **`queue.backfill_symbol_task` and `queue.generate_report_task` remain stubs**, both blocked on
  something other than effort (no object store; no caller).
- **Someone watches during RTH, or the alerting substitutes for a human.** §3.4 and §4.5 both
  reduce what that alerting actually delivers.

---

## 8. Recommended order of work

**Decide first, before any code:** the sizing method for the paper week (§2a). `risk_pct` cannot
produce a passing order on a minute series, so this decides whether day 2 measures anything at
all. Nothing below changes that.

Then:

1. ~~`alerts=alerts` at `main.py:261` — one line (§3.4)~~ **Done** — with §3.4a, without
   which the first thing it sent would have been a lie. The gate is `data_is_current`,
   not `market_open`; see the status block at the top for why the prescription as
   written moves the false all-clear to the opening bell.
2. ~~Read `config.bar_timeframe` in `scripts/preflight.py` — one line plus removing the
   flag (§3.3)~~ **Done** — the flag is kept as an explicit what-if with no default, and
   the series is now named in every verdict measured on it.
3. `reduces_position` exemption on `MaxPositionSizeRule` and `MaxExposureRule` (§3.2)
4. `KillSwitchRule` reads the settled book (§3.1)
5. Throttle the flap path in both stream adapters (§3.5)
6. `_stop_is_missing` does the arithmetic rather than the membership check (§4.2)
7. Stamp the streamed bar from the configured timeframe, or narrow the dropdown (§4.1)
8. Repair day 1's lost bars with an explicit ranged `scripts/backfill_bars.py` run rather than
   waiting for the nightly sweep, and fix the intraday gap expectation (§4.12)
9. `realised_pnl`'s sign, and a P&L section on the daily report (§4.7, §4.8)
10. Reconcile `docs/paper-week/day-1-review.md` with the tree, and write the ADR (§5)

Items 1–4 are what stand between the current commit and a day 2 that can be believed. Items 1
and 2 are done; **3 and 4 are still open, and they are one change rather than two.**

The investigation for them found both prescriptions unsafe as written, which is why they are
not a diff to rush:

- **§3.2's exemption opens a hole.** `reduces_position` is quantity-blind by design
  (`rules.py:52-55`), so with a working `BUY 100` against a flat book a `SELL 300` reads as a
  reduction and skips `max_position_size` entirely — an uncapped short approved by the rule
  whose whole job is capping what an order leaves behind. The predicate these two rules
  actually want is "does this leave behind *more* of the symbol than is held now", which
  subsumes the exemption and keeps its teeth on a reversal.
- **§3.1 is not `KillSwitchRule`-local.** `reduces_position` is asked of the projected book by
  *three* rules, and all three were reproduced by execution: the kill switch approves a short
  entry during a halt, `DailyLossLimitRule` approves an entry past a breached daily-loss
  limit, and `BuyingPowerRule` approves a buy the account cannot pay for. The fix is that
  `RiskRule.check` sees both books — the committed one for a ceiling, the settled one for a
  permission — which is a change to a Protocol exported from `atp_core.risk` and needs an ADR
  distinguishing it from the shape ADR 0020 rejected (`0020:121-125`, "passing `pending` to
  every rule").
- **This order of work is backwards.** Landing 3 before 4 gives the position cap the same
  projected-book defect and the two then have to be untangled. Land the two-book mechanism
  first, or land both in one diff.

---

## 9. What this audit did not cover

Stated so the absence is a recorded judgement rather than an oversight:

- **The integration and e2e suites were not run** — they need Postgres and Redis, and this
  container has neither. Only `tests/unit` was executed.
- **Nothing was run against a live or paper Alpaca endpoint.** CLAUDE.md §1.7 forbids it from
  tests, and this audit respected that; every claim about venue behaviour is derived from the
  adapter code and day 1's logs.
- **The stored `worker_config` row on the operator's host was not read.** §2a's sizing analysis
  uses `WorkerConfig`'s defaults and day 1's recorded revision 4. Substitute the real row before
  acting on it — the conclusion (that `risk_pct` cannot fit a minute series) holds for every
  value above about seven ten-thousandths of a percent, but the exact numbers will differ.
- **`METRICS_TOKEN` on the host cannot be checked from the repository**, and §7 of the review
  contradicts itself about whether it was set.
