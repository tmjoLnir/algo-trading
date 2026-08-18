/**
 * What you are exposed to — layout item 5, and above the signal feed on
 * purpose: what you hold matters more than what the system is thinking about.
 *
 * Two rules from docs/DASHBOARD.md shape this table.
 *
 * **Distance-to-stop, not just the stop price.** The server sends the fraction
 * of the entry-to-stop distance still standing: 1.0 at the entry, 0.0 at the
 * stop. It is rendered as a bar as well as a number, because "how close is this
 * to being closed" is a question about proportion and a reader should not have
 * to do the subtraction. Negative means price is already *through* an unfired
 * stop, which gets its own treatment rather than clamping to zero — clamping
 * would render the most alarming row on the screen as an ordinary one.
 *
 * **Never show a price without its age.** The `Last` column is the price the
 * book was marked at, and it is as old as the book. When the socket has
 * delivered a newer tick for that symbol it is shown beside it, marked live,
 * rather than written over the mark — the P&L next to it was computed from the
 * mark, and quietly swapping the price would put two instants in one row.
 */

import {
  UNKNOWN,
  directionArrow,
  formatDecimal,
  formatMoney,
  formatPercent,
  signOf,
  toneFor,
} from '@/lib/money'
import type { LiveQuote } from '@/hooks/useLiveDashboard'
import type { PositionView } from '@/api/types'

interface Props {
  positions: PositionView[]
  quotes?: Record<string, LiveQuote>
}

/** Where a position sits between its entry and its stop, as a bar. */
function StopGauge({ fraction }: { fraction: string | null }) {
  if (fraction === null) {
    return <span className="text-xs text-slate-600">no stop</span>
  }
  const value = Number(fraction) // geometry only — never a displayed figure
  const through = value < 0
  const width = Math.max(0, Math.min(1, value)) * 100
  const tone = through ? 'bg-rose-500' : value < 0.34 ? 'bg-amber-500' : 'bg-emerald-500'

  return (
    <div className="flex items-center gap-2">
      <div className="h-1.5 w-16 overflow-hidden rounded bg-slate-800">
        <div className={`h-full ${tone}`} style={{ width: `${width}%` }} />
      </div>
      <span
        className={`tabular-nums ${through ? 'font-semibold text-rose-400' : 'text-slate-400'}`}
      >
        {formatPercent(fraction, { places: 0 })}
      </span>
      {through ? <span className="text-xs font-semibold text-rose-400">THROUGH STOP</span> : null}
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

export default function PositionsTable({ positions, quotes = {} }: Props) {
  return (
    <section className="rounded border border-slate-800 bg-slate-900/40">
      <div className="flex items-baseline justify-between px-4 py-3">
        <h2 className="text-sm font-semibold text-slate-300">Open positions</h2>
        <span className="text-xs text-slate-500">{positions.length} held</span>
      </div>

      {positions.length === 0 ? (
        <p className="px-4 pb-4 text-sm text-slate-500">Flat — no open positions.</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full border-t border-slate-800 text-sm">
            <thead>
              <tr className="bg-slate-900/60">
                <Header align="left">Symbol</Header>
                <Header>Qty</Header>
                <Header>Entry</Header>
                <Header>Last</Header>
                <Header>Value</Header>
                <Header>Unrealised</Header>
                <Header align="left">To stop</Header>
                <Header>Stop</Header>
                <Header>Target</Header>
              </tr>
            </thead>
            <tbody>
              {positions.map((position) => {
                const tone = toneFor(position.unrealized_pnl)
                const quote = quotes[position.symbol]
                const unmarked = position.last_price === null
                return (
                  <tr
                    key={position.symbol}
                    className="border-t border-slate-800/70 hover:bg-slate-800/30"
                  >
                    <td className="px-3 py-2 text-left font-medium text-slate-100">
                      {position.symbol}
                      <span className="ml-2 text-xs text-slate-500">
                        {signOf(position.qty) < 0 ? 'SHORT' : 'LONG'}
                      </span>
                    </td>
                    <td className="px-3 py-2 text-right tabular-nums text-slate-300">
                      {formatDecimal(position.qty)}
                    </td>
                    <td className="px-3 py-2 text-right tabular-nums text-slate-300">
                      {formatMoney(position.avg_entry_price)}
                    </td>
                    <td
                      className={`px-3 py-2 text-right tabular-nums ${
                        // Greyed out when there is no mark at all: the row is
                        // real, its value is not known, and it must not read as
                        // an ordinary line.
                        unmarked ? 'text-slate-600' : 'text-slate-300'
                      }`}
                    >
                      {formatMoney(position.last_price)}
                      {quote ? (
                        <div className="text-xs text-sky-400">
                          <span className="mr-1">●</span>
                          {formatMoney(quote.bid)} / {formatMoney(quote.ask)}
                        </div>
                      ) : null}
                    </td>
                    <td className="px-3 py-2 text-right tabular-nums text-slate-300">
                      {formatMoney(position.market_value)}
                    </td>
                    <td className={`px-3 py-2 text-right tabular-nums ${tone}`}>
                      {position.unrealized_pnl === null ? (
                        UNKNOWN
                      ) : (
                        <>
                          {directionArrow(position.unrealized_pnl)}{' '}
                          {formatMoney(position.unrealized_pnl, { signed: true })}
                          <div className="text-xs">
                            {formatPercent(position.unrealized_pnl_pct, { signed: true })}
                          </div>
                        </>
                      )}
                    </td>
                    <td className="px-3 py-2 text-left">
                      <StopGauge fraction={position.distance_to_stop_pct} />
                    </td>
                    <td className="px-3 py-2 text-right tabular-nums text-slate-400">
                      {formatMoney(position.stop_loss_price)}
                    </td>
                    <td className="px-3 py-2 text-right tabular-nums text-slate-400">
                      {formatMoney(position.take_profit_price)}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}
