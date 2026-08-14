# Glossary

**Adjusted close** — price adjusted for splits and dividends. Backtest on it;
trade on raw.

**ATR (Average True Range)** — average volatility over N bars, including
overnight gaps. The basis for volatility-adaptive stops.

**Backtest** — simulating a strategy on historical data.

**Bar / candle** — OHLCV over a fixed interval. `ts` is the OPEN time here.

**Basis points (bps)** — 1/100th of a percent. 50bps = 0.5%.

**Bracket order** — entry plus attached stop-loss and take-profit.

**Client order ID** — our idempotency key, generated before submit, reused on
retry. Prevents a timeout from becoming a duplicate position.

**Drawdown** — decline from an equity peak. Max drawdown is the worst one; it is
what makes people abandon a strategy at the worst moment.

**Expectancy** — average P&L per trade. The number that decides go/no-go. A 30%
win rate with positive expectancy beats 70% with negative.

**Fill** — an execution. One order may produce many.

**Gross vs net exposure** — gross sums |notional| (longs and shorts add); net is
signed. Limits use gross.

**Lookahead bias** — using information that did not exist at decision time. The
most common way a backtest lies.

**MAE / MFE** — maximum adverse / favourable excursion: the worst and best
unrealised P&L during a trade. Tells you whether stops are placed sensibly.

**Mark to market** — revaluing at current price.

**Paper trading** — live data, simulated money.

**Pattern day trader (PDT)** — US rule: 4+ day trades in 5 business days needs
$25k minimum equity.

**Position sizing** — deciding how much. See RISK.md.

**Profit factor** — gross profit ÷ gross loss. Below 1.0 loses money.

**Sharpe ratio** — excess return per unit of volatility, annualised. Assumes
roughly normal returns, so it flatters strategies with rare large losses.

**Slippage** — difference between expected and actual fill price.

**Sortino ratio** — Sharpe but penalising only downside deviation. Usually fairer.

**Spread** — ask minus bid. A cost you pay on every round trip.

**Survivorship bias** — a universe that excludes companies that failed. Inflates
returns, understates drawdowns, worst exactly in the crises you wanted to test.

**Time in force** — how long an order stays working: DAY, GTC, IOC, FOK.

**Trailing stop** — a stop that follows price favourably and never retreats.

**VWAP** — volume-weighted average price.

**Walk-forward analysis** — optimise on one window, test on the next, roll
forward. The defence against overfitting.

**Warmup** — bars needed before indicators are valid; signals during warmup are
discarded.
