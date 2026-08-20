/**
 * Positions — what the worker last recorded holding, and how long ago.
 *
 * **The dashboard shows the same book from a different place, and the
 * difference is the entire point of this screen.** The dashboard reads what the
 * worker published to Redis; when the worker stops, there is nothing to read
 * and it correctly reports no book at all. The same book is also written to
 * Postgres at every evaluation, and that copy outlives the process. So this
 * screen answers "what am I holding?" at the moment the live one cannot —
 * which is usually the moment somebody is asking.
 *
 * That makes the age the most important thing on the page rather than a
 * footnote. A stored book rendered as though it were current is the failure
 * ADR 0007 exists to prevent, moved from a cache to a table, and this screen
 * would be the one that committed it. So the age leads, and past a threshold it
 * stops being a caption and becomes a warning.
 *
 * Two things it does not do:
 *
 * - **No live quotes.** The dashboard overlays socket ticks beside the mark.
 *   Here every figure is as of one past instant, and a live price next to a P&L
 *   computed hours ago would put two instants in one row — the thing the whole
 *   aggregate-endpoint design exists to prevent.
 * - **No actions.** Closing a position and moving a stop place orders, and
 *   there is one path from an intent to a venue (rule §1.5). The endpoints for
 *   both are still stubs.
 */

import PositionsTable from '@/components/PositionsTable'
import { useStoredBook } from '@/hooks/useStoredBook'
import { UNKNOWN, formatAge, formatDateTime, formatMoney } from '@/lib/money'
import type { AccountView } from '@/api/types'

/**
 * How old a stored book may be before its age is a warning rather than a note.
 *
 * Ten minutes: the worker writes a snapshot every evaluation, and the schedule
 * runs on the minute. A book older than ten of them means the worker has missed
 * several in a row, which is a fact about the *worker* rather than about the
 * market — and it is the fact this screen is being read for.
 */
const STALE_AFTER_SECONDS = 600

function Figure({
  label,
  value,
  tone = 'text-slate-100',
}: {
  label: string
  value: string
  tone?: string
}) {
  return (
    <div className="rounded border border-slate-800 bg-slate-900/40 px-3 py-2">
      <div className="text-xs uppercase tracking-wide text-slate-500">{label}</div>
      <div className={`mt-1 tabular-nums ${tone}`}>{value}</div>
    </div>
  )
}

function Account({ account }: { account: AccountView }) {
  return (
    <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
      <Figure label="Equity" value={formatMoney(account.equity)} />
      <Figure label="Cash" value={formatMoney(account.cash)} />
      <Figure label="Gross exposure" value={formatMoney(account.gross_exposure)} />
      {/* Null when equity is zero: leverage against no capital is undefined,
          and rendering it as 0.00 would read as "unlevered". */}
      <Figure label="Leverage" value={formatDecimalOrDash(account.leverage)} />
    </div>
  )
}

/** Leverage is a bare ratio rather than money, and null is a real answer. */
function formatDecimalOrDash(value: string | null): string {
  return value === null ? UNKNOWN : `${formatMoney(value)}×`
}

export default function Positions() {
  const { data, isLoading, error, fetchedSecondsAgo } = useStoredBook()

  if (isLoading) return <p className="p-8 text-sm text-slate-400">Loading…</p>

  if (error && !data) {
    return (
      <p className="p-8 text-sm text-rose-400">
        Could not read the stored book.
        <span className="mt-1 block text-xs text-rose-300/70">
          Nothing can be concluded from this screen — this is not "you hold nothing".
        </span>
      </p>
    )
  }

  if (!data) return null

  // Never written is not empty. A worker that has never traded has published
  // nothing and stored nothing, and saying "no positions" would tell the reader
  // they are flat (ADR 0007).
  if (data.as_of === null) {
    return (
      <section className="rounded border border-slate-800 bg-slate-900/20 p-8 text-center">
        <p className="text-sm text-slate-300">No book has ever been written.</p>
        <p className="mt-1 text-xs text-slate-500">
          This is not "you hold nothing" — it means no worker has yet recorded what you hold. The
          default posture is not trading, so on a fresh install this is expected.
        </p>
      </section>
    )
  }

  const stale = (data.age_seconds ?? 0) >= STALE_AFTER_SECONDS
  const positions = data.positions ?? []
  // Optional in the generated schema because the server model defaults it.
  // Resolved once, so the branch below compares a list rather than a maybe-list.
  const unmarked = data.account?.unmarked_symbols ?? []

  return (
    <div className="space-y-4">
      {/* The age leads. Every figure below describes one past instant, and how
          far past it is decides whether any of them can be acted on. */}
      <div
        className={`rounded border px-4 py-3 ${
          stale
            ? 'border-amber-700/60 bg-amber-950/30 text-amber-200'
            : 'border-slate-800 bg-slate-900/40 text-slate-300'
        }`}
      >
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <span className="text-sm">
            {stale ? '⚠ ' : ''}
            The worker last recorded this book{' '}
            <span className="font-semibold tabular-nums">{formatAge(data.age_seconds)}</span> ago
            <span className="text-slate-500"> · {formatDateTime(data.as_of)}</span>
          </span>
          <span className="text-xs text-slate-500">
            {/* Two ages, deliberately. A tab that refreshed a second ago against
                a worker that stopped an hour ago is fresh by one measure and
                useless by the other. */}
            this tab refreshed {formatAge(fetchedSecondsAgo)} ago · {data.run_mode}
          </span>
        </div>
        {stale ? (
          <p className="mt-1 text-xs">
            The worker writes a snapshot on every evaluation, so a book this old means it has missed
            several. Treat every figure below as history, not as your current exposure.
          </p>
        ) : null}
      </div>

      {data.account ? <Account account={data.account} /> : null}

      {unmarked.length > 0 ? (
        // Non-empty means equity and exposure above both under-report.
        <p className="rounded border border-amber-700/60 bg-amber-950/30 px-3 py-2 text-xs text-amber-200">
          ⚠ No mark for {unmarked.join(', ')} — every value above understates exposure and equity by
          whatever those are worth.
        </p>
      ) : null}

      {/* The same table the dashboard renders, from the same PositionView rows.
          Both are built by `atp_core.dashboard`'s own expressions, so the two
          screens cannot disagree about what a position looks like. No quotes
          are passed: a live tick beside a mark from hours ago would put two
          instants in one row. */}
      <PositionsTable positions={positions} />

      {data.account ? (
        <p className="text-xs text-slate-500">
          Realised P&amp;L {formatMoney(data.account.realized_pnl, { signed: true })} · unrealised{' '}
          {formatMoney(data.account.unrealized_pnl, { signed: true })} · net exposure{' '}
          {formatMoney(data.account.net_exposure)}.
          {/* Day P&L is absent rather than zero: it is this equity against the
              session's first recorded one, which is a question about the equity
              history and is answered on the dashboard. */}{' '}
          Day P&amp;L is on the dashboard — it needs the session's opening equity, which is a
          question about the history rather than about this snapshot.
        </p>
      ) : null}

      {error ? (
        <p className="rounded border border-amber-700/60 bg-amber-950/30 px-3 py-2 text-xs text-amber-200">
          ⚠ The last refresh failed, so the age above has stopped advancing. Everything on screen is
          the last book that was read successfully.
        </p>
      ) : null}
    </div>
  )
}
