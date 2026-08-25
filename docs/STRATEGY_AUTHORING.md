# Writing a strategy

Two ways, same execution path.

| | Coded | Declarative `RuleSet` |
|---|---|---|
| Where | `strategy/examples/*.py` | YAML/JSON, stored in the DB |
| Use when | logic needs real code | conditions over indicators |
| Editable in the UI | no | yes |
| Reviewable in git | yes | no (audit-logged instead) |

Start declarative. Reach for code when the rules stop expressing what you mean.

Two reference strategies ship, and they are worth reading in this order:
`buy_and_hold` is the shortest complete example and the benchmark every result
is read against; `sma_crossover` is the template for a strategy that computes
something.

## The benchmark

`buy_and_hold` buys once per symbol at the first decidable bar and never sells.
Three things about it are load-bearing, and each is the version that is one line
longer than the tempting one:

- **It fills at the second bar's open**, like everything else. A baseline
  exempted from next-bar fills would be measured at a price nobody could have
  paid — and since every strategy is compared *against* it, flattering it by one
  bar's move understates the whole platform by the same amount.
- **One attempt per symbol, not "enter whenever flat".** With a stop configured,
  the shorter version becomes buy → stopped out → buy again, a re-entry system
  whose results depend on the stop. That is not a fixed baseline.
- **It reads the position, not its own signals.** A signal is a request: it
  fills a bar later and risk can refuse it. Counting emitted signals would have
  a restarted runner double a position it already holds.

It carries no stop and no target, so `risk_pct` cannot size it — there is no
distance to risk against. Size a benchmark run with `equity_pct` or `fixed_qty`,
and remember that a universe of *n* symbols gets *n* full-sized positions.

## The contract

A strategy is a **pure decision function**. It receives market events and
returns `Signal`s. It does not size positions, call brokers, read the clock, or
touch the network — that is what lets the identical object run in backtest,
paper and live with no branch inside it.

```python
@register
class MyStrategy(Strategy):
    name = "my_strategy"
    params_schema = {...}  # drives UI form + validation

    @property
    def warmup_bars(self) -> int:
        return self.params["lookback"] + 1

    def on_bar(self, ctx, bar) -> list[Signal]:
        ...
        return [Signal(...)]
```

### Rules

1. **Deterministic.** Same inputs → same signals. No wall-clock reads, no
   unseeded randomness. Non-determinism makes a backtest unreproducible and
   therefore worthless as evidence.
2. **No state outside `self`** that is not reset by `on_start()`. A runner may
   restart mid-session.
3. **Return `[]`, never `None`.**
4. **Populate `reason` and `indicators`.** They are what the dashboard shows a
   human asking "why is this trade on?", and what makes a losing run diagnosable
   later. Not optional.
5. **Declare `warmup_bars` honestly.** Understate it and your first signals are
   computed on partial indicators.
6. **Crossovers are transitions, not levels.** Compare against the previous bar,
   or you re-fire an entry on every bar the condition holds.
7. **Check `ctx.position()` before entering.** The strategy is responsible for
   not re-entering something it already holds.

## Declarative rule sets

```yaml
name: rsi_mean_reversion
universe: [SPY, QQQ, IWM]
timeframe: 1d

entry_long:
  all:
    - {left: {indicator: rsi, period: 14}, op: "<", right: {value: 30}}
    - {left: {price: close}, op: ">", right: {indicator: sma, period: 200}}
exit:
  any:
    - {left: {indicator: rsi, period: 14}, op: ">", right: {value: 55}}

risk:
  stop_loss:   {type: atr, multiplier: 2.0, period: 14}
  take_profit: {type: fixed_pct, value: 0.06}
  position_size: {type: risk_pct, value: 0.01}

max_concurrent_positions: 3
cooldown_bars: 5
```

The `close > SMA(200)` filter is doing real work: buying oversold conditions in
a downtrend is buying things on their way to zero. Mean reversion needs a trend
filter.

**This example ships.** `atp_core.strategy.examples.rsi_mean_reversion()` returns
it parsed and validated, from the same YAML printed above — so it can be
compiled and backtested without retyping, and the page cannot drift from a spec
that still validates. Note its warmup: 200 bars, driven by the trend filter
rather than by the RSI(14) everyone thinks of as the strategy.

**A stored rule set can be queued like any other strategy** — once one is
stored, which nothing can do yet: `POST /api/v1/strategies` is still a stub and
`StrategyRecord`, the only thing a worker can write, has no `ruleset` field. The
run side landed before the authoring side. `POST
/api/v1/backtests` with its `strategy_id` copies the rules onto the run's own
spec — a snapshot, not a reference — and `build_engine` compiles those rather
than looking the name up in the registry. The copy is the point: a rule set is
editable, so a run that recorded only the name would replay differently after an
edit, with both results filed under one name.

Four things the endpoint refuses up front rather than leaving to a job that
fails minutes later in another process: a declarative row with no rules stored,
params sent alongside one (a rule set takes none — its behaviour is the spec),
rules that no longer compile, and a run whose symbols do not meet the rule set's
`universe`. That last one matters more than it looks: a compiled rule set
ignores symbols outside its universe, so such a run completes, takes no trades,
and reports a flat curve indistinguishable from a strategy that never signalled.

The run still has to be configured with the ATR stop the `risk` block asks for.
A compiled rule set emits no protective level of its own, so without it
`risk_pct` refuses every entry at sizing, with a reason on the result — the
`risk` block is read for warmup and is not yet wired into a run's configuration.

Validation rejects a rule set with no exit condition *and* no stop loss — it
could never close a position.

**Security:** rule sets arrive over HTTP and are untrusted. They are interpreted
over a validated tree. Never `eval()` one.

## Rule compilation

`compile_ruleset(spec)` turns a validated `RuleSet` into an ordinary `Strategy`.
Nothing downstream knows the difference — the backtest engine, the paper runner
and live all drive it through the same path they drive `SmaCrossover` through.
There is no "declarative mode" anywhere past this function.

```python
spec = RuleSet.model_validate(yaml.safe_load(text))
strategy = compile_ruleset(spec)  # a Strategy, not a special case
```

**Warmup.** `spec.required_warmup` walks every condition tree and takes the
deepest requirement — not the sum, since two indicators read the same series.
Three things add to an operand's own period:

| | Costs |
|---|---|
| `offset: 3` | +3 bars — the SMA(20) of three bars ago spans 23 |
| `crosses_above` / `crosses_below` | +1 bar — a crossing compares two bars |
| `rsi`, `atr` | +1 bar — Wilder's smoothing averages *differences* |

The `risk` block's ATR periods count too, and pick up that same Wilder bar. An
ATR(50) stop under an SMA(5) entry needs 51 bars before it can place a level,
and a warmup of 6 would spend the first 45 entries refused at sizing for want of
a stop.

The compiled strategy sizes its history window off the same number, so an
understated warmup is not a cosmetic off-by-one: a window one bar short of what
`rsi` needs answers nothing for the *whole run*, and the rule never fires once.

**Three-valued conditions.** An operand that cannot be computed yet is
`unknown`, not `false`. `none: [rsi(14) < 30]` reads as "not oversold", and
collapsing an unknown RSI to false would make it hold on bar 1 of every run.
Groups settle early where they can — one false child settles an `all`, one true
child settles an `any` — and anything that is not definitely true produces no
signal.

**What compilation refuses.** A spec that asks for something the interpreter
cannot execute fails to compile rather than running with the difference ignored:
an unknown indicator, an indicator with no period, `field:` anything but
`close` (dispatch computes on closes), extra indicator `params`, an empty
condition group, and `flatten_at_close`. Each of these is silent otherwise — a
full equity curve answering a question nobody asked.

**What it does not do.** The `risk` block is not the strategy's, beyond warmup.
A strategy never sizes a position and never places a protective level, which is
why `Strategy` has no field for either; the sizer, `StopManager` and the run's
`stop_config` own those. A compiled rule set emits no `stop_loss_price` of its
own — deriving one here *as well* would give a single level two sources that
could disagree. Passing `spec.risk` into a run's configuration is still manual.

What it does own is every exit that is a matter of counting — the condition
tree, `cooldown_bars`, `max_holding_bars`, `max_concurrent_positions`. Those
need bars and positions and no price level at all.

## Workflow

```
draft → backtesting → paper (≥4 weeks) → live
```

Each gate is enforced by the API. See SAFETY.md for what live additionally
requires.

## Common mistakes

- **Fitting the parameters to the backtest.** See BACKTESTING.md on overfitting.
- **No trend filter on a mean-reversion strategy.** As above.
- **Ignoring the spread on cheap or illiquid names.** A 5c spread on a $2 stock
  is 2.5% per round trip.
- **Trading the open.** The first 5 minutes are the widest spreads and the worst
  fills of the day. Wait, unless the edge is specifically there.
- **Too many symbols too early.** Get one working first.
