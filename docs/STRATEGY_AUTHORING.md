# Writing a strategy

Two ways, same execution path.

| | Coded | Declarative `RuleSet` |
|---|---|---|
| Where | `strategy/examples/*.py` | YAML/JSON, stored in the DB |
| Use when | logic needs real code | conditions over indicators |
| Editable in the UI | no | yes |
| Reviewable in git | yes | no (audit-logged instead) |

Start declarative. Reach for code when the rules stop expressing what you mean.

## The contract

A strategy is a **pure decision function**. It receives market events and
returns `Signal`s. It does not size positions, call brokers, read the clock, or
touch the network — that is what lets the identical object run in backtest,
paper and live with no branch inside it.

```python
@register
class MyStrategy(Strategy):
    name = "my_strategy"
    params_schema = {...}          # drives UI form + validation

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

Validation rejects a rule set with no exit condition *and* no stop loss — it
could never close a position.

**Security:** rule sets arrive over HTTP and are untrusted. They are interpreted
over a validated tree. Never `eval()` one.

## Workflow

```
draft → backtest → paper (≥4 weeks) → live
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
