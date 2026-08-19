/**
 * P&L grouped by one dimension, from `/analytics/attribution`.
 *
 * `exit_reason` is the dimension worth opening first, and it is why this table
 * exists rather than a single P&L number: a strategy whose profit comes
 * entirely from its take-profits while its stops bleed has a stop-placement
 * problem, not a signal problem, and no other view in the platform says so
 * (docs/ANALYTICS.md).
 *
 * `contribution_pct` arrives **already scaled** — it is a percentage, not a
 * fraction — and it is denominated in the period's total *absolute* P&L rather
 * than its net. Against the net, a period whose winners and losers nearly
 * cancel reads as +900% for one strategy and −800% for another; against the
 * absolute total every row lands inside ±100% and the signs still say who
 * helped and who hurt. The bar is drawn from that, so its width is a share of
 * what moved rather than of what was left over.
 *
 * `net_pnl` and `avg_pnl` are decimal strings and go through `money.ts`
 * untouched. `win_rate` and `contribution_pct` are float statistics. The two
 * kinds sit in the same row and never mix.
 */

import { directionArrow, formatMoney, toneFor } from '@/lib/money'
import { formatCount, formatStat, formatStatPercent, statTone } from '@/lib/stats'
import { ATTRIBUTION_DIMENSIONS } from '@/hooks/useAnalytics'
import type { AttributionResponse } from '@/api/types'

/** The dimension's own name for its column, rather than the raw parameter. */
function dimensionLabel(by: string): string {
  return ATTRIBUTION_DIMENSIONS.find((d) => d.value === by)?.label ?? by
}

/**
 * One group's key, as a reader should see it.
 *
 * The hour is the one that needs help. It is a **UTC** hour — grouped on the
 * trade's entry, in the timezone everything is stored in — while every
 * timestamp elsewhere on this screen is rendered in the reader's local time. An
 * unlabelled `14` beside a trade list showing local times invites a comparison
 * between two different clocks, so it says which one it is.
 *
 * The weekday needs no such treatment: a US equity session runs 13:30–21:00 UTC
 * at its widest and never straddles UTC midnight, so its UTC day and its
 * session day are the same day (docs/ANALYTICS.md).
 */
function rowLabel(by: string, key: string): string {
  return by === 'hour' ? `${key}:00 UTC` : key
}

interface Props {
  data: AttributionResponse
}

/** A signed share of the period's movement, as a bar either side of centre. */
function ContributionBar({ pct }: { pct: number }) {
  // Geometry only — this is already a float statistic, and a bar is pixels.
  const width = Math.min(100, Math.abs(pct))
  const negative = pct < 0
  return (
    <div className="flex items-center gap-2">
      <div className="flex h-1.5 w-24 overflow-hidden rounded bg-slate-800">
        <div className="flex w-1/2 justify-end">
          {negative ? <div className="h-full bg-rose-500" style={{ width: `${width}%` }} /> : null}
        </div>
        <div className="w-1/2">
          {negative ? null : (
            <div className="h-full bg-emerald-500" style={{ width: `${width}%` }} />
          )}
        </div>
      </div>
      <span className={`tabular-nums ${statTone(pct)}`}>{formatStat(pct, { places: 1 })}%</span>
    </div>
  )
}

function Header({ children, align = 'right' }: { children: string; align?: 'left' | 'right' }) {
  return (
    <th
      className={`px-3 py-2 text-xs font-medium uppercase tracking-wide text-slate-500 ${
        align === 'left' ? 'text-left' : 'text-right'
      }`}
    >
      {children}
    </th>
  )
}

export default function AttributionTable({ data }: Props) {
  if (data.rows.length === 0) {
    return (
      <p className="px-4 py-6 text-center text-sm text-slate-500">
        Nothing to attribute — no round trips closed in this period.
      </p>
    )
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="bg-slate-900/60">
            <Header align="left">{dimensionLabel(data.by)}</Header>
            <Header>Net P&amp;L</Header>
            <Header>Trades</Header>
            <Header>Win rate</Header>
            <Header>Avg P&amp;L</Header>
            <Header align="left">Contribution</Header>
          </tr>
        </thead>
        <tbody>
          {data.rows.map((row) => (
            <tr key={row.key} className="border-t border-slate-800/70 hover:bg-slate-800/30">
              <td className="px-3 py-2 text-left font-medium text-slate-100">
                {rowLabel(data.by, row.key)}
              </td>
              <td className={`px-3 py-2 text-right tabular-nums ${toneFor(row.net_pnl)}`}>
                {directionArrow(row.net_pnl)} {formatMoney(row.net_pnl, { signed: true })}
              </td>
              <td className="px-3 py-2 text-right tabular-nums text-slate-300">
                {formatCount(row.num_trades)}
              </td>
              <td className="px-3 py-2 text-right tabular-nums text-slate-300">
                {formatStatPercent(row.win_rate, { places: 0 })}
              </td>
              <td className={`px-3 py-2 text-right tabular-nums ${toneFor(row.avg_pnl)}`}>
                {formatMoney(row.avg_pnl, { signed: true })}
              </td>
              <td className="px-3 py-2 text-left">
                <ContributionBar pct={row.contribution_pct} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
