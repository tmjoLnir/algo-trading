/**
 * Says how old what you are looking at is, and gives you the button to re-read.
 *
 * Nothing on this dashboard refreshes on a timer any more (ADR 0022), which
 * makes this component the whole of the freshness story rather than a caption
 * beside it. A screen that is only ever as current as the last time somebody
 * asked has to say so continuously and without being asked, because the reader
 * has no other way to tell four-second-old data from four-hour-old data.
 *
 * **Two ages, not one, because there are two of them.** How long since this
 * browser read, and how far behind the *worker's* book already was when it did.
 * A tab that read a second ago against a worker that stopped publishing an hour
 * ago is fresh by one measure and useless by the other, and collapsing them
 * into a single "updated 2s ago" is exactly the reassurance that would hide it.
 *
 * **The book age shown is the sum of both, and that is the point.** The server
 * can only tell us how old the book was *at the moment we asked*; that number is
 * frozen the instant it arrives. Adding the time since we asked turns it back
 * into a live lower bound on how stale the book is now — which is what makes the
 * warning still fire when a laptop sleeps for four hours with this tab open
 * (docs/LOCAL_HOSTING.md §1). Without the addition the badge would be judging a
 * four-hour-old outage by a number captured before it started.
 */

import { formatAge } from '@/lib/money'
import { useSecondsSince } from '@/hooks/useSecondsSince'

interface Props {
  /** When this browser last read, in epoch ms. Null before the first read. */
  updatedAt: number | null
  isFetching: boolean
  onRefresh: () => void
  /** Past this, an age stops being a caption and becomes a warning. */
  staleAfterSeconds: number
  stale?: boolean
  /** How far behind the worker's book was *when it was read*. Null when none. */
  bookAgeSeconds?: number | null
}

export default function RefreshIndicator({
  updatedAt,
  isFetching,
  onRefresh,
  staleAfterSeconds,
  stale,
  bookAgeSeconds = null,
}: Props) {
  // Ticks here rather than in the page: this is the only thing on the dashboard
  // that has to re-render every second, and dragging the position table and the
  // equity chart along with it would be a real cost to move one caption.
  const ageSeconds = useSecondsSince(updatedAt)

  const overdue = ageSeconds !== null && ageSeconds > staleAfterSeconds
  const color = stale || overdue ? 'text-amber-400' : 'text-slate-400'
  // The lower bound on how old the worker's book is *now* — see the header.
  // A book that has aged past the threshold means the worker has stopped
  // publishing, not that this tab is behind.
  const effectiveBookAge = bookAgeSeconds === null ? null : bookAgeSeconds + (ageSeconds ?? 0)
  const bookLagging = effectiveBookAge !== null && effectiveBookAge > staleAfterSeconds

  return (
    <div className="flex flex-wrap items-center gap-3 text-xs">
      <span className={color}>
        {isFetching
          ? 'Reading…'
          : ageSeconds === null
            ? '—'
            : `Read ${formatAge(ageSeconds)} ago${overdue ? ' — may be out of date' : ''}`}
      </span>
      {bookLagging ? (
        <span className="rounded bg-amber-950/50 px-1.5 py-0.5 font-medium text-amber-400">
          worker book {formatAge(effectiveBookAge)} old
        </span>
      ) : null}
      <span className="text-slate-600">manual refresh — this screen does not update itself</span>
      <button
        onClick={onRefresh}
        className="rounded border border-slate-700 px-2 py-1 text-slate-300 hover:bg-slate-800"
      >
        Refresh now
      </button>
    </div>
  )
}
