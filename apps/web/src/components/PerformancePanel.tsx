/**
 * The metric set from `/analytics/performance`.
 *
 * Three things this panel refuses to do, each of which is the tempting version:
 *
 * **A period with no closed trades does not render as a wall of zeros.**
 * `compute_all` returns 0.0 for every ratio it cannot compute, which is correct
 * as a value and a lie as a display: nineteen zeros read as "flat performance"
 * when what happened is that nothing finished. docs/DASHBOARD.md makes it a
 * rule — a figure we do not know renders as a dash, never as `0` — and the
 * honest form of that rule for a whole panel is a sentence.
 *
 * **The annualisation basis is on screen.** Every ratio below scales with
 * `periods_per_year`, which the server infers from the equity curve's own
 * spacing unless a caller pins it, and getting it wrong is the one way to make
 * all of these wrong while all of them still look plausible
 * (docs/ANALYTICS.md). A reader who disagrees with a Sharpe should be able to
 * check this number before doubting the arithmetic.
 *
 * **`max_drawdown` is labelled as what it is.** The curve behind it steps only
 * when a round trip closes, so it is the drawdown of *realised* P&L and is
 * shallower than what the account actually lived through. The equity chart on
 * the dashboard answers "how bad did it get".
 */

import {
  formatCount,
  formatDuration,
  formatStat,
  formatStatPercent,
  statArrow,
  statTone,
} from '@/lib/stats'
import { formatDateTime } from '@/lib/money'
import type { PerformanceResponse } from '@/api/types'

interface Props {
  data: PerformanceResponse
}

function Metric({
  label,
  value,
  tone = 'text-slate-100',
  arrow,
  hint,
}: {
  label: string
  value: string
  tone?: string
  arrow?: string
  hint?: string
}) {
  return (
    <div className="rounded border border-slate-800 bg-slate-900/40 px-3 py-2" title={hint}>
      <div className="text-xs uppercase tracking-wide text-slate-500">{label}</div>
      <div className={`mt-1 tabular-nums ${tone}`}>
        {arrow ? <span className="mr-1">{arrow}</span> : null}
        {value}
      </div>
    </div>
  )
}

function Group({
  title,
  columns = 3,
  children,
}: {
  title: string
  /** Wide columns at the `lg` breakpoint, so a group fills its last row. */
  columns?: 3 | 4
  children: React.ReactNode
}) {
  return (
    <div>
      <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-400">{title}</h3>
      <div
        className={`grid gap-2 sm:grid-cols-2 ${columns === 4 ? 'lg:grid-cols-4' : 'lg:grid-cols-3'}`}
      >
        {children}
      </div>
    </div>
  )
}

export default function PerformancePanel({ data }: Props) {
  const m = data.metrics
  const numTrades = m.num_trades ?? 0

  return (
    <section className="rounded border border-slate-800 bg-slate-900/20 p-4">
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-sm font-semibold text-slate-300">Performance</h2>
        <span className="text-xs text-slate-500">
          {formatDateTime(data.start)} → {formatDateTime(data.end)}
        </span>
      </div>

      {numTrades === 0 ? (
        // Not a zeroed metric set. "Nothing closed in this period" and "this
        // period made nothing" are different sentences, and only the first one
        // is true here.
        <p className="rounded border border-slate-800 bg-slate-900/40 px-3 py-6 text-center text-sm text-slate-400">
          No round trips closed in this period, so there is nothing to compute a statistic over.
          <span className="mt-1 block text-xs text-slate-500">
            Open positions are not counted — a trade is a position episode that has gone back to
            flat.
          </span>
        </p>
      ) : (
        <div className="space-y-4">
          <Group title="Return">
            <Metric
              label="Total return"
              value={formatStatPercent(m.total_return, { signed: true })}
              tone={statTone(m.total_return)}
              arrow={statArrow(m.total_return)}
              hint="Relative to the period's own starting stake, not to an account balance."
            />
            <Metric
              label="CAGR"
              value={formatStatPercent(m.cagr, { signed: true })}
              tone={statTone(m.cagr)}
              arrow={statArrow(m.cagr)}
              hint="Zero for a sub-period sample — a fortnight is not annualised into a growth rate."
            />
            <Metric
              label="Profit factor"
              value={formatStat(m.profit_factor)}
              hint="Gross profit over gross loss. Above 1 means the winners paid for the losers."
            />
          </Group>

          <Group title="Risk">
            <Metric label="Sharpe" value={formatStat(m.sharpe)} tone={statTone(m.sharpe)} />
            <Metric label="Sortino" value={formatStat(m.sortino)} tone={statTone(m.sortino)} />
            <Metric label="Calmar" value={formatStat(m.calmar)} tone={statTone(m.calmar)} />
            <Metric
              label="Max drawdown (realised)"
              value={formatStatPercent(m.max_drawdown)}
              tone={statTone(m.max_drawdown === 0 ? 0 : -1)}
              hint="Drawdown of realised P&L: the curve steps only when a trade closes, so this is shallower than what the account experienced. The dashboard's equity chart answers that."
            />
            <Metric
              label="Drawdown length"
              value={`${formatCount(m.max_drawdown_duration_days)}d`}
            />
            <Metric label="Volatility" value={formatStatPercent(m.volatility)} />
          </Group>

          <Group title="Trades">
            <Metric label="Closed" value={formatCount(numTrades)} />
            <Metric
              label="Win rate"
              value={formatStatPercent(m.win_rate, { places: 1 })}
              hint="Net of fees. A trade that made $3 gross and paid $4 in commission is a loss."
            />
            <Metric label="Avg hold" value={formatDuration(m.avg_holding_period_hours)} />
            <Metric
              label="Exposure"
              value={formatStatPercent(m.exposure_pct, { places: 1 })}
              hint="Fraction of the period during which the book held something. Overlapping positions are merged, not summed."
            />
            <Metric label="Turnover" value={`${formatStat(m.turnover)}×`} />
            <Metric
              label="Expectancy"
              value={formatStat(m.expectancy, { signed: true })}
              tone={statTone(m.expectancy)}
              arrow={statArrow(m.expectancy)}
            />
          </Group>

          <Group title="Trade extremes" columns={4}>
            <Metric
              label="Avg win"
              value={formatStat(m.avg_win)}
              tone="text-emerald-400"
              arrow="▲"
            />
            <Metric
              label="Avg loss"
              value={formatStat(m.avg_loss)}
              tone="text-rose-400"
              arrow="▼"
            />
            <Metric
              label="Largest win"
              value={formatStat(m.largest_win)}
              tone="text-emerald-400"
              arrow="▲"
            />
            <Metric
              label="Largest loss"
              value={formatStat(m.largest_loss)}
              tone="text-rose-400"
              arrow="▼"
            />
          </Group>

          <p className="mt-4 border-t border-slate-800 pt-3 text-xs text-slate-500">
            Annualised at{' '}
            <span className="tabular-nums text-slate-400">
              {formatCount(data.periods_per_year)}
            </span>{' '}
            periods per year, inferred from {formatCount(data.equity_points)} points of the
            realised-P&L curve. Every ratio above scales with it.
            {/* Stated because these five are money-shaped and are not money. They
              are computed in float space by the same functions the backtest uses
              — deliberately, so a live Sharpe is comparable to a backtested one —
              and dressing them up with the ledger's formatter would claim a
              precision the response does not carry (src/lib/stats.ts). */}
            <span className="mt-1 block">
              Expectancy and the trade extremes are float statistics in the quote currency, not
              ledger figures. Exact per-trade P&L is in the table below.
            </span>
          </p>
        </div>
      )}
    </section>
  )
}
