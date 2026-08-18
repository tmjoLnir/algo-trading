/**
 * Says how old the data is and when it next refreshes.
 *
 * With a 5-minute cadence the user cannot tell fresh data from four-minute-old
 * data by looking. Making the age explicit — and visibly warning once it
 * exceeds the interval — is the difference between a dashboard that is trusted
 * correctly and one that is trusted blindly.
 *
 * Two ages, not one, because there are two of them. `ageSeconds` is how long
 * since this browser last fetched; `bookAgeSeconds` is how far behind the
 * *worker's* book was when it did. A tab that just refreshed against a worker
 * that stopped publishing an hour ago is fresh by one measure and useless by
 * the other, and collapsing them into a single "updated 2s ago" is exactly the
 * reassurance that would hide it.
 */

import { formatAge } from '@/lib/money'

interface Props {
  ageSeconds: number | null
  isFetching: boolean
  onRefresh: () => void
  intervalSeconds: number
  stale?: boolean
  /** How far behind the worker's published book was. Null when there is none. */
  bookAgeSeconds?: number | null
}

export default function RefreshIndicator({
  ageSeconds,
  isFetching,
  onRefresh,
  intervalSeconds,
  stale,
  bookAgeSeconds = null,
}: Props) {
  const overdue = ageSeconds !== null && ageSeconds > intervalSeconds * 1.5
  const color = stale || overdue ? 'text-amber-400' : 'text-slate-400'
  // The book lagging by more than a poll interval means the worker has stopped
  // publishing, not that this tab is behind.
  const bookLagging = bookAgeSeconds !== null && bookAgeSeconds > intervalSeconds

  return (
    <div className="flex flex-wrap items-center gap-3 text-xs">
      <span className={color}>
        {isFetching
          ? 'Refreshing…'
          : ageSeconds === null
            ? '—'
            : `Updated ${ageSeconds}s ago${overdue ? ' — data may be stale' : ''}`}
      </span>
      {bookLagging ? (
        <span className="rounded bg-amber-950/50 px-1.5 py-0.5 font-medium text-amber-400">
          worker book {formatAge(bookAgeSeconds)} old
        </span>
      ) : null}
      <span className="text-slate-600">auto-refresh every {intervalSeconds / 60} min</span>
      <button
        onClick={onRefresh}
        className="rounded border border-slate-700 px-2 py-1 text-slate-300 hover:bg-slate-800"
      >
        Refresh now
      </button>
    </div>
  )
}
