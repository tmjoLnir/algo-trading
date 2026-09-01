/**
 * The risk limits, and where the book stands against each of them.
 *
 * On the Strategies tab rather than a Risk tab of its own, and the placement is
 * the argument: this is the check a person makes *before promoting a strategy*
 * — docs/ROADMAP.md's own words for `/risk/status` — so it belongs beside the
 * list of what exists and what has actually run, not one nav click away from
 * it. A reader deciding whether `sma_crossover` is ready to trade paper wants
 * "has it run" and "how much of the exposure ceiling is already spent" on one
 * screen.
 *
 * **Three states, and they must not look alike.**
 *
 * 1. A book was published: every reading is real.
 * 2. No book was published: the ceilings render and every reading is `—`. This
 *    is ordinary — a worker that is up but not trading publishes nothing — and
 *    it is emphatically *not* a compliant book. The server sends nulls rather
 *    than zeroes for exactly this reason (ADR 0007) and the screen must not
 *    undo that by rendering a null as an empty bar that reads as "0% used".
 * 3. `/status` itself failed: the panel falls back to `/risk/limits`, which
 *    reads config and touches no store, so the ceilings still render with the
 *    reason the readings are missing stated above them.
 *
 * **Colour is never the only signal.** Every row carries a word — `at limit`,
 * `ok`, `not observable`, `no reading` — because a red bar is not a signal a
 * colour-blind reader can use, which docs/DASHBOARD.md makes a rule.
 *
 * The rule name is shown under each label on purpose. A refusal on the Orders
 * tab reads "refused by max_gross_exposure", and a reader has to be able to get
 * from that string to the row that should have predicted it.
 */

import { ApiError } from '@/api/client'
import { useRiskLimits, useRiskStatus } from '@/hooks/useRiskLimits'
import { UNKNOWN, formatAge, formatDecimal, formatPercent } from '@/lib/money'
import type { LimitUsageView, RiskLimitsView } from '@/api/types'

/** What each rule is called on a screen, and the order they read best in. */
const LABEL: Record<string, string> = {
  max_position_size: 'Largest position',
  max_gross_exposure: 'Gross exposure',
  daily_loss_limit: 'Day P&L',
  max_open_positions: 'Open positions',
  rate_limit: 'Order rate',
  stale_data: 'Quote age',
}

/** The one row whose reading is signed — a good day is a positive number. */
const SIGNED = new Set(['daily_loss_limit'])

function formatReading(value: string | null | undefined, unit: string, rule: string): string {
  if (value === null || value === undefined) return UNKNOWN
  if (unit === 'fraction_of_equity') {
    return formatPercent(value, { places: 2, signed: SIGNED.has(rule) })
  }
  if (unit === 'seconds') {
    const seconds = formatDecimal(value, { places: 0 })
    return seconds === UNKNOWN ? UNKNOWN : `${seconds}s`
  }
  return formatDecimal(value, { places: 0 })
}

/**
 * The utilisation bar.
 *
 * Rendered only when there is a reading. An unknown utilisation gets no track
 * at all rather than an empty one: an empty bar and a bar at zero are the same
 * picture, and one of them means "we cannot see the book".
 */
function Bar({ utilisation, atLimit }: { utilisation: string | null; atLimit: boolean | null }) {
  if (utilisation === null) return null
  // Width only — never the reading itself. `Number` here drives a CSS
  // percentage and touches nothing anybody counts money with (src/lib/money.ts).
  const pct = Math.max(0, Math.min(100, Number(utilisation) * 100))
  const tone = atLimit ? 'bg-rose-500' : pct >= 80 ? 'bg-amber-500' : 'bg-emerald-600'
  return (
    <div
      className="mt-1 h-1 w-full overflow-hidden rounded bg-slate-800"
      role="presentation"
      title={`${pct.toFixed(0)}% of the limit`}
    >
      <div className={`h-full ${tone}`} style={{ width: `${pct}%` }} />
    </div>
  )
}

function Verdict({ row }: { row: LimitUsageView }) {
  if (!row.observable) return <span className="text-slate-500">not observable</span>
  if (row.at_limit === null) return <span className="text-slate-500">no reading</span>
  if (row.at_limit) return <span className="font-medium text-rose-400">at limit</span>
  return <span className="text-emerald-400">ok</span>
}

function Row({ row }: { row: LimitUsageView }) {
  return (
    <tr className="border-t border-slate-800/70 align-top hover:bg-slate-800/30">
      <td className="px-3 py-2 text-left">
        <div className="font-medium text-slate-100">{LABEL[row.rule] ?? row.rule}</div>
        {/* The string a refusal on the Orders tab names. */}
        <div className="font-mono text-[11px] text-slate-600">{row.rule}</div>
      </td>
      <td className="px-3 py-2 text-right tabular-nums text-slate-200">
        {formatReading(row.current, row.unit, row.rule)}
        <Bar utilisation={row.utilisation} atLimit={row.at_limit} />
      </td>
      <td className="px-3 py-2 text-right tabular-nums text-slate-400">
        {formatReading(row.ceiling, row.unit, '')}
      </td>
      <td className="px-3 py-2 text-left text-xs">
        <Verdict row={row} />
        {row.note ? <div className="mt-0.5 max-w-md text-slate-500">{row.note}</div> : null}
      </td>
    </tr>
  )
}

/** The ceilings alone, when `/status` could not answer at all. */
function ceilingRows(limits: RiskLimitsView): LimitUsageView[] {
  const of = (rule: string, unit: string, ceiling: string): LimitUsageView => ({
    rule,
    unit,
    ceiling,
    current: null,
    utilisation: null,
    at_limit: null,
    observable: true,
    note: null,
  })
  return [
    of('max_position_size', 'fraction_of_equity', limits.max_position_pct),
    of('max_gross_exposure', 'fraction_of_equity', limits.max_gross_exposure_pct),
    of('daily_loss_limit', 'fraction_of_equity', limits.max_daily_loss_pct),
    of('max_open_positions', 'count', String(limits.max_open_positions)),
    of('rate_limit', 'orders_per_minute', String(limits.max_orders_per_minute)),
    of('stale_data', 'seconds', String(limits.max_quote_age_seconds)),
  ]
}

export default function RiskLimitsPanel() {
  const status = useRiskStatus()
  // Only asked when the primary failed — see the hook for why that is the whole
  // point of `/risk/limits` existing as its own route.
  const fallback = useRiskLimits(status.isError)

  const rows = status.data?.limits ?? (fallback.data ? ceilingRows(fallback.data) : [])

  // Optional in the generated schema because the server model defaults it.
  // Resolved once so the branch below tests a list rather than a maybe-list,
  // the way `StoredRow` handles `universe`.
  const unmarked = status.data?.unmarked_symbols ?? []

  const reason = status.isError
    ? status.error instanceof ApiError
      ? status.error.detail
      : String(status.error)
    : null

  return (
    <section className="rounded border border-slate-800 bg-slate-900/20">
      <div className="flex flex-wrap items-baseline justify-between gap-2 px-4 py-3">
        <div>
          <h2 className="text-sm font-semibold text-slate-300">Risk limits</h2>
          <p className="mt-0.5 text-xs text-slate-500">
            The account-wide ceilings, and where the book stands against them. What to check before
            promoting a strategy.
          </p>
        </div>
        {status.data?.book_published ? (
          <span className="text-xs text-slate-500">
            book {formatAge(status.data.book_age_seconds)} old
          </span>
        ) : null}
      </div>

      {reason ? (
        // The degraded state, said out loud. The ceilings below are still real
        // — they come from config — but nothing is being measured against them.
        <p
          role="alert"
          className="mx-4 mb-3 rounded border border-amber-700/60 bg-amber-950/30 px-3 py-2 text-xs text-amber-200"
        >
          ⚠ Could not read the current usage, so the ceilings below are shown on their own — no
          reading here means <em>unknown</em>, not zero.
          <span className="mt-1 block text-amber-200/70">{reason}</span>
        </p>
      ) : status.data && !status.data.book_published ? (
        <p className="mx-4 mb-3 rounded border border-slate-700 bg-slate-900/60 px-3 py-2 text-xs text-slate-400">
          No worker has published a book, so there is nothing to measure against these ceilings.
          Ordinary on a fresh install and while no strategy is chosen on the <strong>Worker</strong>{' '}
          tab — but it is <em>not</em> the same as a compliant book, which is why every reading
          below is {UNKNOWN} rather than zero.
        </p>
      ) : null}

      {unmarked.length > 0 ? (
        <p className="mx-4 mb-3 rounded border border-amber-700/60 bg-amber-950/30 px-3 py-2 text-xs text-amber-200">
          ⚠ {unmarked.join(', ')} carry no mark, so every exposure figure below
          <strong> understates</strong> — the direction that makes a breached limit look compliant.
        </p>
      ) : null}

      {status.isLoading ? (
        <p className="px-4 py-6 text-center text-sm text-slate-500">Loading…</p>
      ) : rows.length === 0 ? (
        <p className="px-4 py-6 text-center text-sm text-slate-500">
          The limits could not be read at all.
        </p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="bg-slate-900/60">
                <th className="px-3 py-2 text-left text-xs font-medium uppercase tracking-wide text-slate-500">
                  Limit
                </th>
                <th className="px-3 py-2 text-right text-xs font-medium uppercase tracking-wide text-slate-500">
                  Now
                </th>
                <th className="px-3 py-2 text-right text-xs font-medium uppercase tracking-wide text-slate-500">
                  Ceiling
                </th>
                <th className="px-3 py-2 text-left text-xs font-medium uppercase tracking-wide text-slate-500">
                  Status
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <Row key={row.rule} row={row} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}
