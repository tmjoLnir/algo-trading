/**
 * Completed round trips, from `/analytics/trades`.
 *
 * A row is a **position episode** — flat, through however many scale-ins and
 * partial exits, back to flat — not an order and not a tax lot (ADR 0015). That
 * is what makes its exit reason a single answer and its holding period a
 * well-defined window, and it is why an open position appears nowhere on this
 * screen: it has no exit yet, so three of the four things worth knowing about
 * it are undefined.
 *
 * Two rules from docs/ANALYTICS.md are visible in the excursion columns.
 *
 * **No bars means a dash, never a zero.** Zero says the trade never went
 * against us, which is the most flattering possible reading of "we did not
 * measure". A genuine zero on one side *is* a measurement — a trade that only
 * ever went one way — and reads as `0.00` rather than as a dash, which is the
 * distinction the null exists to preserve.
 *
 * **A whole column of dashes says why.** When the request spanned more symbols
 * than the server measures excursions for, that is stated above the table
 * rather than left to be inferred from the nulls — "we did not look" and "there
 * were no bars" are different facts.
 *
 * `exit_reason` is read off the closing order's stored `purpose`, and `unknown`
 * is reachable for real: orders written before migration `c3f8b2d5e714` have no
 * purpose to read. It is deliberately not guessed into a bucket, and it is
 * tinted so a reader can see it was not.
 */

import {
  directionArrow,
  formatDateTime,
  formatDecimal,
  formatMoney,
  formatPercent,
  toneFor,
} from '@/lib/money'
import { formatDuration } from '@/lib/stats'
import type { TradesResponse, TradeView } from '@/api/types'

interface Props {
  data: TradesResponse
}

/**
 * How an exit reason reads. Colour is an accent on text that already names it —
 * every cell states the reason in words (docs/DASHBOARD.md).
 */
const EXIT_TONE: Record<string, string> = {
  stop_loss: 'text-rose-400',
  take_profit: 'text-emerald-400',
  signal: 'text-slate-300',
  time: 'text-slate-400',
  manual: 'text-amber-400',
  unknown: 'text-amber-400',
}

const EXIT_HINT: Record<string, string> = {
  unknown:
    'Stored before the order table carried a purpose. Not guessed into a bucket — a wrong exit reason is worse than a missing one.',
  manual: 'An operator or the runbook, not the strategy.',
}

function Header({
  children,
  align = 'right',
  hint,
}: {
  children: string
  align?: 'left' | 'right'
  hint?: string
}) {
  return (
    <th
      title={hint}
      className={`px-3 py-2 text-xs font-medium uppercase tracking-wide text-slate-500 ${
        align === 'left' ? 'text-left' : 'text-right'
      }`}
    >
      {children}
    </th>
  )
}

function Row({ trade }: { trade: TradeView }) {
  const tone = toneFor(trade.net_pnl)
  return (
    <tr className="border-t border-slate-800/70 align-top hover:bg-slate-800/30">
      <td className="px-3 py-2 text-left font-medium text-slate-100">
        {trade.symbol}
        <span className="ml-2 text-xs text-slate-500">{trade.side.toUpperCase()}</span>
        <div className="text-xs text-slate-500">{trade.strategy_id}</div>
      </td>
      <td className="px-3 py-2 text-right tabular-nums text-slate-300">
        {formatMoney(trade.entry_price)}
        <div className="text-xs text-slate-500">{formatDateTime(trade.entry_ts)}</div>
      </td>
      <td className="px-3 py-2 text-right tabular-nums text-slate-300">
        {formatMoney(trade.exit_price)}
        <div className="text-xs text-slate-500">{formatDateTime(trade.exit_ts)}</div>
      </td>
      <td className="px-3 py-2 text-right tabular-nums text-slate-300">
        {formatDecimal(trade.qty)}
      </td>
      <td className={`px-3 py-2 text-right tabular-nums ${tone}`}>
        {directionArrow(trade.net_pnl)} {formatMoney(trade.net_pnl, { signed: true })}
        <div className="text-xs">{formatPercent(trade.return_pct, { signed: true })}</div>
      </td>
      <td className="px-3 py-2 text-right tabular-nums text-slate-400">
        {formatMoney(trade.fees)}
      </td>
      {/* Null and zero say different things here, and `formatMoney` already
          renders the first as a dash. MFE is >= 0 and MAE is <= 0 by
          construction, so neither is signed. */}
      <td className="px-3 py-2 text-right tabular-nums text-emerald-400/80">
        {formatMoney(trade.max_favorable_excursion)}
      </td>
      <td className="px-3 py-2 text-right tabular-nums text-rose-400/80">
        {formatMoney(trade.max_adverse_excursion)}
      </td>
      <td
        className={`px-3 py-2 text-left ${EXIT_TONE[trade.exit_reason] ?? 'text-slate-300'}`}
        title={EXIT_HINT[trade.exit_reason]}
      >
        {trade.exit_reason}
      </td>
      <td className="px-3 py-2 text-right tabular-nums text-slate-400">
        {formatDuration(trade.holding_period_hours)}
      </td>
    </tr>
  )
}

export default function TradesTable({ data }: Props) {
  if (data.trades.length === 0) {
    return (
      <p className="px-4 py-6 text-center text-sm text-slate-500">
        No round trips closed in this period.
        <span className="mt-1 block text-xs">
          A position still open is not a trade — it appears on the dashboard, not here.
        </span>
      </p>
    )
  }

  return (
    <>
      {data.excursions_omitted ? (
        // Why the two excursion columns are empty. Left unsaid, a column of
        // dashes reads as "no bars stored" rather than as "we did not look".
        <p className="mx-4 mb-3 rounded border border-amber-700/60 bg-amber-950/30 px-3 py-2 text-xs text-amber-200">
          ⚠ MAE and MFE were not measured: this period spans more symbols than the server measures
          excursions for in one request. Narrow the period or filter to one strategy.
        </p>
      ) : null}

      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="bg-slate-900/60">
              <Header align="left">Symbol</Header>
              <Header>Entry</Header>
              <Header>Exit</Header>
              <Header>Qty</Header>
              <Header>Net P&amp;L</Header>
              <Header>Fees</Header>
              <Header hint="Maximum favourable excursion: the best unrealised P&L reached during the trade, in money. Zero or greater by construction.">
                MFE
              </Header>
              <Header hint="Maximum adverse excursion: the worst unrealised P&L reached during the trade, in money. Zero or less by construction — the number to read before deciding a stop sits too close.">
                MAE
              </Header>
              <Header align="left">Exit</Header>
              <Header>Held</Header>
            </tr>
          </thead>
          <tbody>
            {data.trades.map((trade) => (
              <Row key={trade.trade_id} trade={trade} />
            ))}
          </tbody>
        </table>
      </div>

      <p className="px-4 py-3 text-xs text-slate-500">
        {data.trades.length} closed round {data.trades.length === 1 ? 'trip' : 'trips'}, newest
        first. MFE and MAE are bounds at bar resolution, not measurements — the bar covering an
        entry may have printed its extreme before the fill, so both err towards a larger excursion.
      </p>
    </>
  )
}
